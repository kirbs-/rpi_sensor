# import asyncio
import logging
import traceback
import argparse
import subprocess
import sys
# logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='%(asctime)s:%(levelname)s:%(message)s')

from rpi2mqtt.config import Config
from rpi2mqtt.binary import *
from rpi2mqtt.temperature import *
from rpi2mqtt.ibeacon import Scanner
from rpi2mqtt.switch import Switch
from rpi2mqtt.thermostat import HestiaPi
import time

try:
    from beacontools import BeaconScanner, IBeaconFilter
except:
    print("Unable to load beacontools")


# setup CLI parser
parser = argparse.ArgumentParser()
parser.add_argument("-c", "--config",
                help="Path to config.yaml")

parser.add_argument('-d', '--dry-run', 
                help='Test drive config without triggering callbacks.')

parser.add_argument('--generate-config',
                help="Generate config.yaml template.",
                action='store_true')

parser.add_argument('--install-service',
                help='Install rpi2mqtt as systemd service.',
                action='store_true')


def main():
    config = None
    args = parser.parse_args() 

    if args.generate_config:
        Config.generate_config('config.yaml')
        sys.exit(0)

    if args.install_service:
        username = input("User to run service as [pi]: ") or 'pi'
        # _path = input("Path rpi2mqtt executable (run `which rpi2mqtt`): ")
        _path = subprocess.check_output(['which', 'rpi2mqtt']).decode().strip()
        install_service(username, _path)
        sys.exit(0)

    scanner = None

    if args.config:
        config = Config.get_instance(filename=args.config)

    if not config:
        logging.error("No configuration file present.")
        sys.exit(1)

    # start MQTT client
    from rpi2mqtt.mqtt import MQTT
    MQTT.setup()

    sensor_list = []
    if len(config.sensors) >0:
        for sensor in config.sensors:
            s = None
            if sensor.type == 'dht22':
                s = DHT(sensor.pin, sensor.topic, sensor.name, 'sensor', sensor.type)
            elif sensor.type == 'ibeacon':
                s = Scanner(sensor.name, sensor.topic, sensor.uuid, sensor.away_timeout)
            elif sensor.type == 'switch':
                s = Switch(sensor.name, sensor.pin, sensor.topic)
            elif sensor.type == 'reed':
                s = ReedSwitch(sensor.name, sensor.pin, sensor.topic, sensor.normally_open, sensor.get('device_type'))
            elif sensor.type == 'bme280':
                s = BME280(sensor.name, sensor.topic)
            elif sensor.type == 'hestiapi':
                s = HestiaPi(sensor.name, sensor.topic, sensor.heat_setpoint, sensor.cool_setpoint, dry_run=args.dry_run)
            elif sensor.type == 'onewire':
                s = OneWire(sensor.name, sensor.topic)
            else:
                logging.warn('Sensor {} found in config, but was not setup.'.format(sensor.name))
            if s:
                sensor_list.append(s)

        try:
            scanner = BeaconScanner(sensor_list[1].process_ble_update) # TODO update to search sensor list and setup scanner accordingly.
            scanner.start()
        except:
            logging.error("Beacon scanner did not start")
    else:
        logging.warn("No sensors defined in {}".format(args.config))

    try:
        while True:

            for sensor in sensor_list:
                sensor.callback()

            MQTT.ping_subscriptions()
            time.sleep(config.polling_interval)

    except:
        traceback.print_exc()
        MQTT.client.loop_stop()

        if scanner:
            scanner.stop()


def install_service(username, _path):
    template = """[Unit]
Description=rpi2mqtt Service
After=network-online.target

[Service]
# replace user with an existing system user
Restart=on-failure
User={username}
ExecStart={_path}

[Install]
WantedBy=multi-user.target
    """.format(username=username, _path=_path)
    # return template
    with open('/etc/systemd/system/rpi2mqtt.service', 'w') as f:
        f.write(template)

if __name__ == '__main__':
    main()