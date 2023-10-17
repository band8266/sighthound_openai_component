"""Person detection using Sighthound cloud service."""
import io
import logging
import base64
from pathlib import Path

import openai
from PIL import Image, ImageDraw, UnidentifiedImageError
import simplehound.core as hound
import voluptuous as vol

from homeassistant.components.image_processing import (
    CONF_ENTITY_ID,
    CONF_NAME,
    CONF_SOURCE,
    PLATFORM_SCHEMA,
    ImageProcessingEntity,
)
from homeassistant.const import ATTR_ENTITY_ID, CONF_API_KEY
from homeassistant.core import split_entity_id
import homeassistant.helpers.config_validation as cv
import homeassistant.util.dt as dt_util
from homeassistant.util.pil import draw_box

_LOGGER = logging.getLogger(__name__)

EVENT_PERSON_DETECTED = "sighthound.person_detected"
EVENT_VEHICLE_DETECTED = "sighthound.vehicle_detected"

ATTR_BOUNDING_BOX = "bounding_box"
ATTR_PEOPLE = "people"
ATTR_VEHICLE = "vehicle"
ATTR_PLATE = "plate"
ATTR_MAKE = "make"
ATTR_MODEL = "model"
ATTR_COLOR = "color"
ATTR_REGION = "region"
ATTR_VEHICLE_TYPE = "vehicle_type"
CONF_ACCOUNT_TYPE = "account_type"
CONF_SAVE_FILE_FOLDER = "save_file_folder"
CONF_SAVE_TIMESTAMPTED_FILE = "save_timestamped_file"
CONF_ALWAYS_SAVE_LATEST_JPG = "always_save_latest_jpg"
DATETIME_FORMAT = "%Y-%m-%d_%H:%M:%S"
DEV = "dev"
PROD = "prod"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_API_KEY): cv.string,
        vol.Optional(CONF_ACCOUNT_TYPE, default=DEV): vol.In([DEV, PROD]),
        vol.Optional(CONF_SAVE_FILE_FOLDER): cv.isdir,
        vol.Optional(CONF_SAVE_TIMESTAMPTED_FILE, default=False): cv.boolean,
        vol.Optional(CONF_ALWAYS_SAVE_LATEST_JPG, default=False): cv.boolean,
    }
)


def setup_platform(hass, config, add_entities, discovery_info=None):
    """Set up the platform."""
    # Validate credentials by processing image.
    api_key = config[CONF_API_KEY]
    account_type = config[CONF_ACCOUNT_TYPE]
    api = hound.cloud(api_key, account_type)
    try:
        api.detect(b"Test")
    except hound.SimplehoundException as exc:
        _LOGGER.error("Sighthound error %s setup aborted", exc)
        return

    save_file_folder = config.get(CONF_SAVE_FILE_FOLDER)
    if save_file_folder:
        save_file_folder = Path(save_file_folder)

    entities = []
    for camera in config[CONF_SOURCE]:
        sighthound_person = SighthoundPersonEntity(
            api,
            camera[CONF_ENTITY_ID],
            camera.get(CONF_NAME),
            save_file_folder,
            config[CONF_SAVE_TIMESTAMPTED_FILE],
            config[CONF_ALWAYS_SAVE_LATEST_JPG],
        )
        entities.append(sighthound_person)

        sighthound_vehicle = SighthoundVehicleEntity(
            api,
            camera[CONF_ENTITY_ID],
            camera.get(CONF_NAME),
            save_file_folder,
            config[CONF_SAVE_TIMESTAMPTED_FILE],
            config[CONF_ALWAYS_SAVE_LATEST_JPG],
        )
        entities.append(sighthound_vehicle)
    add_entities(entities)


class SighthoundPersonEntity(ImageProcessingEntity):
    """Create a sighthound person entity."""

    def __init__(
        self, api, camera_entity, name, save_file_folder, save_timestamped_file, always_save_latest_jpg
    ):
        """Init."""
        self._api = api
        self._camera = camera_entity
        if name:
            self._name = name
        else:
            camera_name = split_entity_id(camera_entity)[1]
            self._name = f"sighthound_person_{camera_name}"
        self._state = None
        self._last_detection = None
        self._image_width = None
        self._image_height = None
        self._save_file_folder = save_file_folder
        self._save_timestamped_file = save_timestamped_file
        self._always_save_latest_jpg = always_save_latest_jpg
        self._age = []
        self._gender = []
        self._bounding_box = []

    def process_image(self, image):
        """Process an image."""
        self._age = []
        self._gender = []
        self._bounding_box = []
        detections = self._api.detect(image)
        people = hound.get_people(detections)
        faces = hound.get_faces(detections)
        self._state = len(people)
        if self._state > 0:
            self._last_detection = dt_util.now().strftime(DATETIME_FORMAT)

        metadata = hound.get_metadata(detections)
        self._image_width = metadata["image_width"]
        self._image_height = metadata["image_height"]
        for person in people:
            self.fire_person_detected_event(person)
        if self._save_file_folder:
            if self._state > 0 or self._always_save_latest_jpg:
                self.save_image(image, people, self._save_file_folder)
        for face in faces:
            self._age.append(face["age"])
            self._gender.append(face["gender"])

    def fire_person_detected_event(self, person):
        """Send event with detected total_persons."""
        self.hass.bus.fire(
            EVENT_PERSON_DETECTED,
            {
                ATTR_ENTITY_ID: self.entity_id,
                ATTR_BOUNDING_BOX: hound.bbox_to_tf_style(
                    person["boundingBox"], self._image_width, self._image_height
                ),
            },
        )

    def save_image(self, image, people, directory):
        """Save a timestamped image with bounding boxes around targets and send it to OpenAI for image editing."""
        try:
            img = Image.open(io.BytesIO(bytearray(image))).convert("RGB")
        except UnidentifiedImageError:
            _LOGGER.warning("Sighthound unable to process image, bad data")
            return
        latest_save_path = directory / f"{self._name}_latest.png"
        img.save(latest_save_path)
        if self._save_timestamped_file:
            timestamp_save_path = directory / f"{self._name}_{self._last_detection}.png"
            img.save(timestamp_save_path)
            _LOGGER.info("Sighthound saved file %s", timestamp_save_path)

        mask_img = Image.new("L", img.size, 0)  
        draw = ImageDraw.Draw(mask_img)

        for person in people:
            left = person["boundingBox"]["x"]
            top = person["boundingBox"]["y"]
            right = left + person["boundingBox"]["width"] 
            bottom = top + person["boundingBox"]["height"] 
            box_draw = (left, top, right, bottom)
            draw.rectangle(box_draw, fill=255)

        img.putalpha(mask_img)
##        mask_save_path = directory / f"{self._name}_mask.png"
##        img.save(mask_save_path)

        edit = openai.Image.create_edit(
            image=open(mask_save_path, "rb"),
            size="1024x1024",
            response_format="b64_json",
            prompt="A person standing on a doorstep, with a creepy grim reeper behind them.",
            api_key=""
    )
        image_data = edit['data'][0]
        image_bytes = base64.b64decode(image_data['b64_json'])
        with io.BytesIO(image_bytes) as image_stream:
            final_image = Image.open(image_stream)
            final_save_path = directory.joinpath(f"{self._name}_final.png")
            final_image.save(final_save_path)
            if self._save_timestamped_file:
                timestamp_AI_path = directory / f"{self._name}_AI_{self._last_detection}.png"
                img.save(timestamp_AI_path)


    @property
    def camera_entity(self):
        """Return camera entity id from process pictures."""
        return self._camera

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def should_poll(self):
        """Return the polling state."""
        return False

    @property
    def state(self):
        """Return the state of the entity."""
        return self._state

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return ATTR_PEOPLE

    @property
    def extra_state_attributes(self):
        """Return the attributes."""
        attr = {}
        attr.update({"last_person": self._last_detection})
        attr.update({"gender": self._gender})
        attr.update({"age": self._age})
        attr.update({"boundingbox": self._bounding_box})
        return attr


class SighthoundVehicleEntity(ImageProcessingEntity):
    """Create a sighthound vehicle entity."""

    def __init__(
        self, api, camera_entity, name, save_file_folder, save_timestamped_file, always_save_latest_jpg
    ):
        """Init."""
        self._api = api
        self._camera = camera_entity
        if name:
            self._name = name
        else:
            camera_name = split_entity_id(camera_entity)[1]
            self._name = f"sighthound_vehicle_{camera_name}"
        self._state = None
        self._last_detection = None
        self._image_width = None
        self._image_height = None
        self._save_file_folder = save_file_folder
        self._save_timestamped_file = save_timestamped_file
        self._always_save_latest_jpg = always_save_latest_jpg
        self._plates = []
        self._color = []
        self._make = []
        self._model = []
        self._vehicle_type = []

    def process_image(self, image):
        """Process an image."""
        self._plates = []
        self._color = []
        self._make = []
        self._model = []
        self._vehicle_type = []
        detections = self._api.recognize(image, "vehicle,licenseplate")
        vehicles = hound.get_vehicles(detections)
        self._state = len(vehicles)
        if self._state > 0:
            self._last_detection = dt_util.now().strftime(DATETIME_FORMAT)

        metadata = hound.get_metadata(detections)
        self._image_width = metadata["image_width"]
        self._image_height = metadata["image_height"]
        for vehicle in vehicles:
            self.fire_vehicle_detected_event(vehicle)
            self._plates.append(vehicle["licenseplate"])
            self._color.append(vehicle["color"])
            self._make.append(vehicle["make"])
            self._model.append(vehicle["model"])
            self._vehicle_type.append(vehicle["vehicleType"])
        if self._save_file_folder:
            if self._state > 0 or self._always_save_latest_jpg:
                self.save_image(image, vehicles, self._save_file_folder)

    def fire_vehicle_detected_event(self, vehicle):
        """Send event."""
        self.hass.bus.fire(
            EVENT_VEHICLE_DETECTED,
            {
                ATTR_ENTITY_ID: self.entity_id,
                ATTR_PLATE: vehicle["licenseplate"],
                ATTR_VEHICLE_TYPE: vehicle["vehicleType"],
                ATTR_MAKE: vehicle["make"],
                ATTR_MODEL: vehicle["model"],
                ATTR_COLOR: vehicle["color"],
                ATTR_REGION: vehicle["region"],
                ATTR_BOUNDING_BOX: hound.bboxvert_to_tf_style(
                    vehicle["boundingBox"], self._image_width, self._image_height
                ),
            },
        )

    def save_image(self, image, vehicles, directory):
        """Save a timestamped image with bounding boxes around targets."""
        try:
            img = Image.open(io.BytesIO(bytearray(image))).convert("RGB")
        except UnidentifiedImageError:
            _LOGGER.warning("Sighthound unable to process image, bad data")
            return
        draw = ImageDraw.Draw(img)

        for vehicle in vehicles:
            box = hound.bboxvert_to_tf_style(
                vehicle["boundingBox"], self._image_width, self._image_height
            )
            draw_box(draw, box, self._image_width, self._image_height)

        latest_save_path = directory / f"{self._name}_latest.jpg"
        img.save(latest_save_path)

        if self._save_timestamped_file:
            timestamp_save_path = directory / f"{self._name}_{self._last_detection}.jpg"
            img.save(timestamp_save_path)
            _LOGGER.info("Sighthound saved file %s", timestamp_save_path)

    @property
    def camera_entity(self):
        """Return camera entity id from process pictures."""
        return self._camera

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def should_poll(self):
        """Return the polling state."""
        return False

    @property
    def state(self):
        """Return the state of the entity."""
        return self._state

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return ATTR_VEHICLE

    @property
    def extra_state_attributes(self):
        """Return the attributes."""
        attr = {}
        attr.update({"last_vehicle": self._last_detection})
        attr.update({"plates": self._plates})
        attr.update({"color": self._color})
        attr.update({"make": self._make})
        attr.update({"model": self._model})
        attr.update({"vehicle_type": self._vehicle_type})
        return attr