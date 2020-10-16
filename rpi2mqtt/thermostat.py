from rpi2mqtt.switch import BasicSwitch
from rpi2mqtt.base import Sensor
import rpi2mqtt.mqtt as mqtt
from rpi2mqtt.temperature import BME280
import RPi.GPIO as GPIO
import pendulum
import logging
import json


class HVAC(object):
    HEAT_PUMP = {
        'fan': 18,
        'compressor': 23,
        'reversing_valve': 12,
        'aux': 16,
    }

    HEAT_PUMP_MODES = {
        'off': [],
        'fan': [HEAT_PUMP['fan']],
        'heat': [HEAT_PUMP['fan'], HEAT_PUMP['compressor']],
        'cool': [HEAT_PUMP['fan'], HEAT_PUMP['compressor'], HEAT_PUMP['reversing_valve']],
        'aux': [HEAT_PUMP['fan'], HEAT_PUMP['compressor'], HEAT_PUMP['aux']],
    }

    ON = 'ON'
    OFF = 'OFF'

    HEAT = 'heat'
    COOL = 'cool'
    AUX = 'aux'
    AUTO = 'auto'
    OFF = 'off'


class HvacException(Exception):
    pass


class HestiaPi(Sensor):

    def __init__(self, name, topic, heat_setpoint, cool_setpoint, set_point_tolerance=1.0, min_run_time=15):
        # self._modes = HVAC.HEAT_PUMP_MODES
        super(HestiaPi, self).__init__(name, None, topic, 'climate', 'HestiaPi')
        self.mode = 'heat'
        # self.active = False
        # self.desired_mode = 'off'
        self.active_start_time = None
        self.set_point_cool = cool_setpoint
        self.set_point_heat = heat_setpoint
        # how much wiggle room in temperature reading before starting/stopping HVAC.
        # setting this too low can trigger frequence HVAC cycles.
        self.set_point_tolerance = set_point_tolerance
        # Minimum time HVAC should run (in minutes)
        self.min_run_time = min_run_time
        # how soon can HVAC be activated again after stopping (in minutes)
        self.min_trigger_cooldown_time = 15
        self.last_mode_change_time = None
        self.last_hvac_state_change_time = None
        self.bme280 = None
        # container to holder mode switches. Do not use directly.
        self._modes = {}
        # super(HestiaPi, self).__init__(name, None, topic, 'climate', 'HestiaPi')
        self.setup()

    def setup(self):
        logging.debug('Setting up HestiaPi')
        self.bme280 = BME280(self.name, self.topic)

        for mode, pins in HVAC.HEAT_PUMP_MODES.items():
            switch = BasicSwitch(self.name, pins, '{}_{}'.format(self.topic, mode), mode)
            # switch.setup()
            self._modes[mode] = switch

        # setup GPIO inputs on HVAC pins
        GPIO.setmode(GPIO.BCM)
        for capability, pin in HVAC.HEAT_PUMP.items():
            GPIO.setup(pin, GPIO.IN)

        # Subscribe to MQTT command topics
        mqtt.subscribe(self.mode_command_topic, self.mqtt_set_mode_callback)
        mqtt.subscribe(self.temperature_set_point_command_topic, self.mqtt_set_temperature_set_point_callback)
        mqtt.subscribe(self.fan_command_topic, self.mqtt_set_fan_state_callback)

    @property
    def mode_command_topic(self):
        return '{}/mode/set'.format(self.topic)

    @property
    def temperature_set_point_command_topic(self):
        return '{}/temperature/set'.format(self.topic)

    @property
    def fan_command_topic(self):
        return '{}/fan/set'.format(self.topic)

    @property
    def homeassistant_mqtt_config_topic(self):
        return 'homeassistant/{}/{}/config'.format('climate', self.name)

    @property
    def homeassistant_mqtt_config(self):
        return {
                'name': '{}_{}'.format(self.name, self.device_class),
                'unique_id': '{}_{}_{}_rpi2mqtt'.format(self.name, self.device_model, self.device_class),
                "json_attributes_topic": self.topic,
                'device': self.device_config,
                'min_temp': 65,
                'max_temp': 80,
                'initial': 72,
                'modes': ['off', 'auto', 'heat', 'cool', 'aux'],
                'fan_modes': ['auto','high'],
                'action_topic': self.topic,
                'action_template': '{{ value_json.hvac_state }}',
                'current_temperature_topic': self.topic,
                'current_temperature_template': '{{ value_json.current_temperature | round(1) }}',
                'mode_state_topic': self.topic,
                'mode_state_template': '{{ value_json.mode }}',
                'mode_command_topic': self.mode_command_topic, 
                'temperature_state_topic': self.topic,
                'temperature_state_template': '{{ value_json.set_point }}',
                'temperature_command_topic': self.temperature_set_point_command_topic,
                'fan_modes': ['auto', 'on'],
                'fan_mode_state_topic': self.topic,
                'fan_mode_state_template': '{{ value_json.fan_state }}',
                'fan_mode_command_topic': self.fan_command_topic
            }

    def set_state(self, mode, state):
        if state == HVAC.ON:
            self.active_start_time = pendulum.now()
            self._modes[mode].on()

            # confirm mode change
            if mode == self.hvac_state:
                logging.info('Turned {} {}.'.format(mode, state))
            else:
                logging.warn('Did not set HVAC state to {}. Try again.'.format(mode))

        elif state == HVAC.OFF:
            self._modes[mode].off()
            self.active_start_time = None

            # confirm mode change
            if 'off' == self.hvac_state:
                logging.info('Turned {} {}.'.format(mode, state))
            else:
                logging.warn('Did not set HVAC state to {}. Try again.'.format(mode))
        else:
            raise HvacException("State '{}' is not a valid state.".format(state))
        
        self.last_hvac_state_change_time = pendulum.now()

    @property
    def active_time(self):
        if self.active:
            try:
                return (pendulum.now() - self.active_start_time).in_minutes() 
            except Exception as e:
                logging.exception(e)
        return 0

    @property
    def active(self):
        return self.hvac_state in  [HVAC.COOL, HVAC.HEAT, HVAC.AUX]

    @property
    def minutes_since_last_mode_change(self):
        # if self.active:
        if self.last_mode_change_time:
            return (pendulum.now() - self.last_mode_change_time).in_minutes() 
        else:
            return 1000

    @property
    def minutes_since_last_hvac_state_change(self):
        if self.last_hvac_state_change_time:
            return (pendulum.now() - self.last_hvac_state_change_time).in_minutes() 
        else:
            return 1000

    @property
    def set_point(self):
        if self.mode == HVAC.HEAT:
            return self.set_point_heat
        elif self.mode == HVAC.COOL:
            return self.set_point_cool

    @property
    def hvac_state(self):
        """Current HVAC mode based on active GPIO pins."""
        active_pins = []
        # read pin state
        for capability, pin in HVAC.HEAT_PUMP.items():
            if GPIO.input(pin):
                active_pins.append(pin)
        active_pins = set(active_pins)

        # search heat pump modes for a match
        for mode, p in HVAC.HEAT_PUMP_MODES.items():
            if active_pins == set(p):
                logging.debug('HVAC mode is "{}". Active GPIO pins = {}'.format(mode, active_pins))
                return mode

    @property
    def temperature(self):
        temp = self.bme280.state()['temperature']
        return temp

    def state(self):
        data = self.bme280.state()
        return {
            'bme280': data,
            'mode': self.mode,
            'active_time': self.active_time,
            'active': self.active,
            'hvac_state': self.hvac_state,
            'heat_setpoint': self.set_point_heat,
            'cool_setpoint': self.set_point_cool,
            'set_point': self.set_point,
            'current_temperature': self.bme280.state()['temperature'],
            'humidity': self.bme280.state()['humidity'],
            'pressure': self.bme280.state()['pressure'],
        }

    def payload(self):
        return json.dumps(self.state())

    def callback(self, *args):
        # system active, should we turn it off?
        logging.debug('Checking temperature...temp = {}, heat_setpoint = {}, cool_setpoint = {}, set_point_tolerance = {}'.format(self.temperature, self.set_point_heat, self.set_point_cool, self.set_point_tolerance))
        if self.active:
            # if heating is current temperature above set point?
            if self.mode == 'heat' and self.temperature > self.set_point_heat + self.set_point_tolerance:
                # turn hvac off
                logging.info('Temperature is {}. Turning heat off.'.format(self.temperature))
                self.off()
            elif self.mode == 'cool' and self.temperature < self.set_point_cool - self.set_point_tolerance:
                # turn hvac off
                logging.info('Temperature is {}. Turning cool off.'.format(self.temperature))
                self.off()
        else:
            if self.mode == 'heat' and self.temperature < self.set_point_heat - self.set_point_tolerance:
                # turn hvac on
                logging.info('Temperature is {}. Turning heat on.'.format(self.temperature))
                self.on()
            elif self.mode == 'cool' and self.temperature > self.set_point_cool + self.set_point_tolerance:
                # turn hvac on
                logging.info('Temperature is {}. Turning cool on.'.format(self.temperature))
                self.on()
            # system is inactive, should we turn it on?
        logging.info('HVAC is {}. Mode is {}. Temperature is {}.'.format(self.active, self.mode, self.temperature))
        mqtt.publish(self.topic, self.payload())

    # def mode_is_changeable(self):
    #     """Can thermostat active mode be chagned?"""
    #     # stop changes from cooling to heat or vice versa while system is running

    #     minutes_since_last_mode_change = (pendulum.now - self.last_mode_change_time).in_minutes()
    #     return not self.active and self.active_time >= self.min_run_time and self.minutes_since_last_mode_chage >= self.min_trigger_cooldown_time


    def mode(self, mode):
        if mode in HVAC.HEAT_PUMP_MODES:
            self.mode = mode
            self.last_mode_change_time = pendulum.now()
        else:
            raise IllegalArgumentException('{} mode is not a valid HVAC mode'.format(mode))

    def _can_change_hvac_state(self):
        """Don't change HVAC state from heat to cool or vice versa if the system is running."""
        if (self.hvac_state == 'cool' and self.mode == 'heat') or (self.hvac_state == 'heat' and self.mode == 'cool'): 
            logging.warn("Don't change between heating and cooling. Doing so may damage your system.")
        elif self.active and self.active_time <= self.min_run_time:
            logging.warn("System needs to run for atleast {} minutes. Only running for {} minutes.".format(self.min_run_time, self.active_time))
        elif not self.active and self.minutes_since_last_hvac_state_change <= self.min_run_time:
            logging.warn("System needs to idle for atleast {} minutes. Only idle for {} minutes.".format(self.min_run_time, self.minutes_since_last_hvac_state_change))
        elif self.minutes_since_last_mode_change <= self.min_trigger_cooldown_time:
            logging.warn("Can only change mode every {} minutes. It's been {} minutes since last change.".format(self.min_trigger_cooldown_time, self.minutes_since_last_mode_change))
        # elif self.mode == self.hvac_state:
        #     logging.info('Ignoring mode change since HVAC is alread in {} mode'.format(self.mode))
        else:
            return True

    def on(self):
        """Helper to turn HVAC and capture useful logging."""
        if self._can_change_hvac_state():
            self.set_state(self.mode, HVAC.ON)
            # TODO verify state was changed and publish result to MQTT
        else:
            logging.warn('Did not activate {}'.format(self.mode))

    def off(self):
        if self._can_change_hvac_state():
            self.set_state(self.mode, HVAC.OFF)
        else:
            logging.warn("Did not deactivate {}.".format(self.mode))

    def mqtt_set_temperature_set_point_callback(self, client, userdata, message):
        try:
            logging.info("Received temperature set point update request: {}".format(message.payload))
            payload = float(message.payload.decode())
            if self.mode == HVAC.HEAT:
                self.set_point_heat = payload
            else:
                self.set_point_cool = payload
        except Exception as e:
            logging.error('Unable to proces message.', e)

        mqtt.publish(self.topic, self.payload())

    def mqtt_set_fan_state_callback(self, fan_state):
        pass

    def mqtt_set_mode_callback(self, mode):
        try:
            logging.info("Received HVAC mode update request: {}".format(message))
            payload = message.payload.decode()
            self.mode(payload)
        except Exception as e:
            logging.error('Unable to proces message.', e)

        mqtt.publish(self.topic, self.payload())




    