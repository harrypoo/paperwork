#    Paperwork - Using OCR to grep dead trees the easy way
#    Copyright (C) 2014  Jerome Flesch
#
#    Paperwork is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    Paperwork is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with Paperwork.  If not, see <http://www.gnu.org/licenses/>.

import base64
import configparser
import datetime
import logging
import uuid

import pyinsane2

from paperwork_backend.config import PaperworkConfig
from paperwork_backend.config import PaperworkSetting
from paperwork_backend.config import paperwork_cfg_boolean
from paperwork_backend.util import find_language


logger = logging.getLogger(__name__)
DEFAULT_CALIBRATION_RESOLUTION = 200
RECOMMENDED_SCAN_RESOLUTION = 300


class _ScanTimes(object):

    """
    Helper to find, load and rewrite the scan times stored in the configuration
    """
    __ITEM_2_CONFIG = {
        'calibration': ('Scanner', 'ScanTimeCalibration'),
        'normal': ('Scanner', 'ScanTime'),
        'ocr': ('OCR', 'OCRTime'),
    }

    def __init__(self):
        self.section = self.__ITEM_2_CONFIG['normal'][0]
        self.values = {}
        self.value = self

    def load(self, config):
        for (k, cfg) in self.__ITEM_2_CONFIG.items():
            try:
                value = float(config.get(cfg[0], cfg[1]))
                self.values[k] = value
            except (configparser.NoOptionError, configparser.NoSectionError):
                if k in self.values:
                    self.values.pop(k)

    def update(self, config):
        for (k, v) in self.values.items():
            if k not in self.__ITEM_2_CONFIG:
                logger.warning("Got timing for '%s' but don't know how to"
                               " store it" % k)
                continue
            cfg = self.__ITEM_2_CONFIG[k]
            config.set(cfg[0], cfg[1], str(v))

    def __getitem__(self, item):
        if item in self.values:
            return self.values[item]
        return 60.0

    def __setitem__(self, item, value):
        self.values[item] = value

    def __get_value(self):
        return self


class _PaperworkScannerCalibration(object):
    def __init__(self, section):
        self.section = section
        self.value = None

    def load(self, config):
        try:
            pt_a_x = int(config.get(
                "Scanner", "Calibration_Pt_A_X"))
            pt_a_y = int(config.get(
                "Scanner", "Calibration_Pt_A_Y"))
            pt_b_x = int(config.get(
                "Scanner", "Calibration_Pt_B_X"))
            pt_b_y = int(config.get(
                "Scanner", "Calibration_Pt_B_Y"))
            if (pt_a_x > pt_b_x):
                (pt_a_x, pt_b_x) = (pt_b_x, pt_a_x)
            if (pt_a_y > pt_b_y):
                (pt_a_y, pt_b_y) = (pt_b_y, pt_a_y)

            resolution = DEFAULT_CALIBRATION_RESOLUTION
            try:
                resolution = int(config.get(
                    "Scanner", "Calibration_Resolution"))
            except (configparser.NoOptionError, configparser.NoSectionError):
                logger.warning("Calibration resolution is not specified in the"
                               " configuration. Will assume the calibration"
                               " was done with a resolution of %ddpi"
                               % resolution)

            self.value = (resolution, ((pt_a_x, pt_a_y), (pt_b_x, pt_b_y)))
        except (configparser.NoOptionError, configparser.NoSectionError):
            # no calibration -> no cropping -> we have to keep the whole image
            # each time
            self.value = None

    def update(self, config):
        if self.value is None:
            return
        config.set("Scanner", "Calibration_Resolution",
                   str(self.value[0]))
        config.set("Scanner", "Calibration_Pt_A_X",
                   str(self.value[1][0][0]))
        config.set("Scanner", "Calibration_Pt_A_Y",
                   str(self.value[1][0][1]))
        config.set("Scanner", "Calibration_Pt_B_X",
                   str(self.value[1][1][0]))
        config.set("Scanner", "Calibration_Pt_B_Y",
                   str(self.value[1][1][1]))


class _PaperworkDate(object):
    def __init__(self, section, token,
                 default_value_func=lambda: datetime.datetime.today()):
        self.section = section
        self.setting = PaperworkSetting(section, token)
        self.default_value_func = default_value_func
        self.value = default_value_func()
        self.date_format = "%Y-%m-%d"

    def load(self, config):
        self.setting.load(config)
        value = self.setting.value
        if value is None:
            self.value = self.default_value_func()
            return
        dt = datetime.datetime.strptime(value, self.date_format)
        self.value = dt

    def update(self, config):
        if self.value is None:
            value = None
        else:
            value = self.value.strftime(self.date_format)
        self.setting.value = value
        self.setting.update(config)


class _PaperworkLangs(object):
    """
    Convenience setting. Gives all the languages used as one dictionary
    """

    def __init__(self, ocr_lang_setting, spellcheck_lang_setting):
        self.ocr_lang_setting = ocr_lang_setting
        self.spellcheck_lang_setting = spellcheck_lang_setting
        self.section = "OCR"

    def __get_langs(self):
        ocr_lang = self.ocr_lang_setting.value
        if ocr_lang is None:
            return None
        return {
            'ocr': ocr_lang,
            'spelling': self.spellcheck_lang_setting.value
        }

    value = property(__get_langs)

    @staticmethod
    def load(_):
        pass

    @staticmethod
    def update(_):
        pass


class _PaperworkSize(object):
    def __init__(self, section, base_token,
                 default_size=(1024, 768),
                 min_size=(400, 300)):
        self.section = section
        self.base_token = base_token
        self.value = default_size
        self.default_size = default_size
        self.min_size = min_size

    def load(self, config):
        try:
            w = config.get(self.section, self.base_token + "_w")
            w = int(w)
            if w < self.min_size[0]:
                w = self.min_size[0]
            h = config.get(self.section, self.base_token + "_h")
            h = int(h)
            if h < self.min_size[1]:
                h = self.min_size[1]
            self.value = (w, h)
            return
        except (configparser.NoOptionError, configparser.NoSectionError):
            self.value = self.default_size

    def update(self, config):
        config.set(self.section, self.base_token + "_w", str(self.value[0]))
        config.set(self.section, self.base_token + "_h", str(self.value[1]))


class _PaperworkRecent(object):
    def __init__(self, section, base_token, max_length=10):
        self.section = section
        self.base_token = base_token
        self.max_length = max_length
        self.value = []

    def push(self, value):
        value = value.strip()
        if value == "":
            return
        if value in self.value:
            # duplicates are useless
            return
        self.value.append(value)
        if len(self.value) > self.max_length:
            self.value.pop(0)

    def load(self, config):
        self.value = []
        try:
            idx = 0
            while True:
                v = config.get(self.section, self.base_token + "_" + str(idx))
                v = v.encode("utf-8")
                v = base64.decodebytes(v)
                v = v.decode("utf-8")
                self.push(v)
                idx += 1
        except (configparser.NoSectionError, configparser.NoOptionError):
            pass

    def update(self, config):
        for (idx, v) in enumerate(self.value):
            v = v.encode("utf-8")
            v = base64.encodebytes(v).strip()
            v = v.decode("utf-8")
            config.set(self.section, self.base_token + "_" + str(idx), v)


def get_default_spellcheck_lang(ocr_lang):
    ocr_lang = ocr_lang.value
    if ocr_lang is None:
        return None

    # Try to guess the lang based on the ocr lang
    lang = find_language(ocr_lang)
    if hasattr(lang, 'iso639_1_code'):
        return lang.iso639_1_code
    if hasattr(lang, 'alpha2'):
        return lang.alpha2
    return lang.alpha_2


def load_config():
    config = PaperworkConfig()

    settings = {
        'main_win_size': _PaperworkSize("GUI", "main_win_size"),
        'ocr_enabled': PaperworkSetting("OCR", "Enabled", lambda: True,
                                        paperwork_cfg_boolean),
        'result_sorting': PaperworkSetting(
            "GUI", "Sorting", lambda: "scan_date"
        ),
        'scanner_calibration': _PaperworkScannerCalibration("Scanner"),
        'scanner_devid': PaperworkSetting("Scanner", "Device"),
        'scanner_resolution': PaperworkSetting(
            "Scanner", "Resolution",
            lambda: RECOMMENDED_SCAN_RESOLUTION, int
        ),
        'scanner_source': PaperworkSetting("Scanner", "Source"),
        'scanner_has_feeder': PaperworkSetting(
            "Scanner", "Has_Feeder",
            lambda: False,
            paperwork_cfg_boolean),
        'scan_time': _ScanTimes(),
        'zoom_level': PaperworkSetting("GUI", "zoom_level",
                                       lambda: 0.0, float),

        # activation
        'activation_email': PaperworkSetting(
            "Activation", "email", lambda: None, str
        ),
        'activation_key': PaperworkSetting(
            "Activation", "key", lambda: None, str
        ),
        'first_start': _PaperworkDate("Activation", "first_start"),

        # update detection
        'check_for_update': PaperworkSetting("Update", "check", lambda: False,
                                             paperwork_cfg_boolean),
        'last_update_check': _PaperworkDate(
            "Update", "last_check",
            lambda: datetime.datetime(year=1970, month=1, day=1)
        ),
        'last_update_found': PaperworkSetting(
            "Update", "last_update_found", lambda: None, str
        ),

        # statistics
        'send_statistics': PaperworkSetting("Statistics", "send", lambda: False,
                                            paperwork_cfg_boolean),
        'last_statistics_post': _PaperworkDate(
            "Statistics", "last_post",
            lambda: datetime.datetime(year=1970, month=1, day=1)
        ),
        'uuid': PaperworkSetting(
            "Statistics", "uuid", lambda: uuid.getnode(), int
        ),

        # recents
        'last_documents': _PaperworkRecent("recent", "documents", 3),
        'last_searches': _PaperworkRecent("recent", "search", 10),
    }

    ocr_lang = get_default_spellcheck_lang
    settings['spelling_lang'] = (
        PaperworkSetting("SpellChecking", "Lang",
                         lambda: ocr_lang(config.settings['ocr_lang']))
    )
    settings['langs'] = (
        _PaperworkLangs(config.settings['ocr_lang'], settings['spelling_lang'])
    )

    config.settings.update(settings)
    return config


def _get_scanner(config, devid, preferred_sources=None):
    logger.info("Will scan using %s" % str(devid))

    dev = pyinsane2.Scanner(name=devid)

    config_source = config['scanner_source'].value

    if 'source' not in dev.options:
        logger.warning("Can't set the source on this scanner. Option not found")
    elif preferred_sources:
        pyinsane2.set_scanner_opt(dev, 'source', preferred_sources)
    elif config_source:
        pyinsane2.set_scanner_opt(dev, 'source', [config_source])

    resolution = int(config['scanner_resolution'].value)
    logger.info("Will scan at a resolution of %d" % resolution)

    if 'resolution' not in dev.options:
        logger.warning("Can't set the resolution on this scanner."
                       " Option not found")
    else:
        try:
            pyinsane2.set_scanner_opt(dev, 'resolution', [resolution])
        except pyinsane2.PyinsaneException as exc:
            resolution = int(dev.options['resolution'].value)
            logger.warning("Failed to set resolution. Will fall back to the"
                           " current one: {}".format(resolution))
            logger.exception(exc)

    if 'mode' not in dev.options:
        logger.warning("Can't set the mode on this scanner. Option not found")
    else:
        try:
            pyinsane2.set_scanner_opt(dev, 'mode', ['Color'])
        except pyinsane2.PyinsaneException as exc:
            logger.warning("Failed to set scan mode: {}".format(exc))
            logger.exception(exc)

    try:
        pyinsane2.maximize_scan_area(dev)
    except pyinsane2.PyinsaneException as exc:
        logger.warning("Failed to maximize scan area: {}".format(exc))
        logger.exception(exc)
    return (dev, resolution)


def get_scanner(config, preferred_sources=None):
    devid = config['scanner_devid'].value

    try:
        return _get_scanner(config, devid, preferred_sources)
    except (KeyError, pyinsane2.PyinsaneException) as exc:
        logger.warning("Exception while configuring scanner: %s: %s"
                       % (type(exc), exc))
        logger.exception(exc)
        try:
            # we didn't find the scanner at the given ID
            # but maybe there is only one, so we can guess the scanner to use
            devices = [
                x for x in pyinsane2.get_devices()
                if x.name[:4] != "v4l:"
            ]
            if len(devices) != 1:
                raise
            logger.info("Will try another scanner id: %s" % devices[0].name)
            return _get_scanner(config, devices[0].name, preferred_sources)
        except pyinsane2.PyinsaneException:
            # this is a fallback mechanism, but what interest us is the first
            # exception, not the one from the fallback
            raise exc
