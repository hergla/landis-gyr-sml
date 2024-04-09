#!/usr/bin/python3

"""
Landis+Gyr Stromzaehler auslesen und an Influx und/oder Graphite 
senden. 
Der Graphite Part ist auskommentiert (siehe Main)

Die gelesenen Werte werden zunaechst in einen lokalen Redis abgelegt.
Die Weiterletung an Influx erfolgt in einem seperaten Thread.

Der Landis Gyr Stromzaehler liefert im default nur die Werte
fuer die Zaehlerstaende (Bezug und Einspeisung.)
Der Werte fuer den aktuellen Verbrauch wird erst geliefert, wenn der Zaehler
entsprechend eingestellt ist. 
Fuer die Freigabe ist evtl. eine PIN erfoderlich. Diese bekommst du 
von deinem Versorger/Netzbetreiber.

"""

import serial
import argparse
import sys
import time
from datetime import datetime
import redis
import graphyte
from smllib  import SmlStreamReader
from smllib.const import OBIS_NAMES, UNITS
from hexdump import hexdump
from threading import Thread
import influxdb_client, os, time
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
import pytz


verbose = 0 
GRAPHITEHOST = 'oel.localdomain'
INFLUX_INI = 'influx.ini'
INFLUX_BUCKET = 'Energie'
TZ =  pytz.timezone('Europe/Berlin')

OBIS_MAP_GRAPHITE = { '0100010800ff' : 'Verbrauch.total',
                      '0100020800ff' : 'Einspeisung.total',
                      '0100100700ff' : 'Wirkleistung.aktuell' }

def dump(info, data, cond=True):
    if cond and data:
        print(f'{info}:')
        hexdump(data) 


def open_serial(device):
    try:
        fd = serial.Serial(device, 9600, timeout=2+1)
    except serial.SerialException as e:
        print(f"Exception: {e}")
        return None
    return fd


def read_sml(ser):
    """Read the next SML transport message from the serial device
   
    Returns
    -------
    bytes
        On succes: The complete SML transport message.
    None
        On failure
    """

    sml_frame = None # default result
    max_read = 5 #limit the number of read attemps to avoid endless loop

    escapeSequence = b'\x1b\x1b\x1b\x1b'
    startMessage = b'\x01\x01\x01\x01'
    startSyn = escapeSequence + startMessage
    endMessageB1 = b'\x1a'
    endMsg = escapeSequence + endMessageB1
    start_found = False
    end_found = False
    # Falls es beim lesen zu timeout kommt. siehe openSerial() timeout 
    d_start = b""
    while not start_found and max_read > 0:
        d_start = ser.read_until(startSyn)
        dump('d_start', d_start, verbose>=1)
        if d_start == startSyn:   # Start Sequence am Anfang.
            start_found = True
        elif not d_start == startSyn and len(d_start)>8: # Start mittendrin ?
            if verbose >= 1: print('mittendrin...')
            if startSyn in d_start:
                pos = d_start.find(startSyn)
                d_start = d_start[pos:]
                start_found = True
            else:
                print('Was das denn...')
        else:
            print("read timeout")
        max_read -= 1
    max_read = 5
    # Rest ohne timeout ?
    
    d_frame = ser.read_until(endMsg)
    dump('d_frame', d_frame, verbose>=1)

    d_end = ser.read(3)   # 1 Byte count filler. 2 Bytes CRC
    sml_frame = d_start + d_frame + d_end
    return sml_frame


def dosml(data):
    stream = SmlStreamReader()
    stream.add(data) 
    sml_frame = stream.get_frame()
    if not sml_frame:
        print('Bytes missing')
        return None 

    parsed_msgs = sml_frame.parse_frame()
    # In the parsed message the obis values are typically found like this
    obis_values = parsed_msgs[1].message_body.val_list

    ts = datetime.now().timestamp()
    for  list_entry in obis_values:
        if list_entry.obis in OBIS_NAMES:
            #print(f'{OBIS_NAMES[list_entry.obis]} - {list_entry.obis} {list_entry.obis.obis_short}: ', end ='')
            value = str(round(list_entry.value * ( 10 ** list_entry.scaler),1))
            #print(f'value: {value} {UNITS[list_entry.unit]}')
            if list_entry.obis in OBIS_MAP_GRAPHITE:
                graphite_frame = f'Strom.{OBIS_MAP_GRAPHITE[list_entry.obis]} {value} {ts}'
           #     print (f'graphite_frame: {graphite_frame}')
           #     print("=" * 80)
                yield graphite_frame

        #print(list_entry.obis)            # 0100010800ff
        #print(list_entry.obis.obis_code)  # 1-0:1.8.0*255
        #print(list_entry.obis.obis_short) # 1.8.0
        #print(list_entry.value)
        #print(list_entry.scaler)          # Wert = value * 10 ** scaler
        #print(list_entry.unit)            # DLMS-Unit-List, zu finden beispielsweise in IEC 62056-62.

class SendGraphite(Thread):
    def __init__(self):
        Thread.__init__(self)
        self.redis_con = redis.Redis(host='localhost')
        self.graphite_con = graphyte.Sender(GRAPHITEHOST, raise_send_errors=True)

    def run(self):
        while True:
            stromwert = self.redis_con.rpop('stromwert')
            if not stromwert:
                time.sleep(1.0)
            else:
                stromwert = stromwert.decode()
                #print(f'send graphite: {stromwert}')
                if not self.sendgraphite(stromwert):
                    self.redis_con.rpush('stromwert', stromwert)
                    time.sleep(2)

    def sendgraphite(self, stromwert):
        (metric, value, timestamp) = stromwert.split()
        try:
            self.graphite_con.send(metric, float(value), float(timestamp))
        except Exception as e:
            print(e)
            return False
        return True


class SendInflux(Thread):
    def __init__(self, inifile):
        Thread.__init__(self)
        self.bucket = INFLUX_BUCKET
        self.redis_con = redis.Redis(host='localhost')
        self.client = influxdb_client.InfluxDBClient.from_config_file(inifile)
        self.write_api = self.client.write_api(write_options=SYNCHRONOUS)

    def run(self):
        while True:
            stromwert = self.redis_con.rpop('stromwertinfluxdb')
            if not stromwert:
                time.sleep(1)
            else:
                stromwert = stromwert.decode()
                (metric, value, timestamp) = stromwert.split()
                (messurement, tagval, field) = metric.split('.')

                ts = datetime.fromtimestamp(float(timestamp)).astimezone(TZ).isoformat()
                # print(ts, messurement, tagval, field, value)
                point = (
                         Point(messurement)
                         .tag('Wert', tagval)
                         .field(field, float(value))
                         .time(ts)
                         )
                if not self.sendinflux(point):
                    self.redis_con.rpush('stromwertinfluxdb', stromwert)
                    time.sleep(2)

    def sendinflux(self, point):
        try:
            #print(f'point: {point}')
            self.write_api.write(bucket=self.bucket, record=point)
        except Exception as e:
            print(e)
            return False
        return True

################################ MAIN #################################
def main():
    global verbose
    error = None

    parser = argparse.ArgumentParser(description='Read Landis+Gyr E320 electric power meter')
    parser.add_argument('-d', '--device', '--port', required=True, help='name of serial port device, e.g. /dev/ttyUSB0')
    parser.add_argument('-i', '--inifile', required=True, help='Influx Configfile.')
    parser.add_argument('-v', '--verbose', action='count', help='verbosity level')
    args = parser.parse_args()

    if args.verbose:
        verbose = args.verbose

    #sendgraphite = SendGraphite()
    #sendgraphite.start()
    sendinflux = SendInflux(inifile=args.inifile)
    sendinflux.start()

    fdser = open_serial(args.device)
    if fdser:
        redis_con = redis.Redis(host='localhost')
        while True:
            smlframe = read_sml(fdser)        
            if smlframe:      
                dump("SMLTransportMessage", smlframe, verbose >=1)
                for graphite_frame in dosml(smlframe):
                    #print(f'lpush redis: {graphite_frame}')
                    #redis_con.lpush('stromwert', graphite_frame)
                    redis_con.lpush('stromwertinfluxdb', graphite_frame)
            else:
                error = "ERR_MESG"
    else:
        error = "ERR_DEVICE"

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")

    if error:
        print(now, error)

if __name__ == "__main__":
    main()
