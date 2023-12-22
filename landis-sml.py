#!/usr/bin/python3

"""Read and decode the SML output of the Landis+Gyr ED320 electric power meter.
Extract the current power consumption and the total energy consumption.

## SML Protocol References

* [Technische Richtlinie BSI TR-03109-1](https://www.bsi.bund.de/SharedDocs/Downloads/DE/BSI/Publikationen/TechnischeRichtlinien/TR03109/TR-03109-1_Anlage_Feinspezifikation_Drahtgebundene_LMN-Schnittstelle_Teilb.pdf?__blob=publicationFile) - the main specification
* [DLMS blue book](https://www.dlms.com/files/Blue-Book-Ed-122-Excerpt.pdf) - contains OBIS codes and measurement units
* [EDI@Energy Codeliste der OBIS-Kennzahlen fuer den deutschen Energiemarkt](https://www.edi-energy.de/index.php?id=38&tx_bdew_bdew%5Buid%5D=64&tx_bdew_bdew%5Baction%5D=download&tx_bdew_bdew%5Bcontroller%5D=Dokument&cHash=d2cc24364c4712ad83676043d5cc02f5)
* [Beschreibung SML Datenprotokoll fuer SMART METER](http://itrona.ch/stuff/F2-2_PJM_5_Beschreibung%20SML%20Datenprotokoll%20V1.0_28.02.2011.pdf)


"""

import serial
import argparse
import sys
import datetime
from smllib  import SmlStreamReader
from hexdump import hexdump

verbose = 0 

def dump(info, data, cond=True):
    if cond and data:
        print(f'{info}:')
        hexdump(data) 


def open_serial(device):
    try:
        result = serial.Serial(device, 9600, timeout=2+1)
    except serial.SerialException as e:
        print(f"Exception: {e}")
        return None
    return result


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
    if sml_frame is None:
        print('Bytes missing')

# A quick Shortcut to extract all values without parsing the whole frame
# In rare cases this might raise an InvalidBufferPos exception, then you have to use sml_frame.parse_frame()
    obis_values = sml_frame.get_obis()

# return all values but slower
    parsed_msgs = sml_frame.parse_frame()
    for msg in parsed_msgs:
        # prints a nice overview over the received values
        print(msg.format_msg())

# In the parsed message the obis values are typically found like this
    obis_values = parsed_msgs[1].message_body.val_list

# The obis attribute of the SmlListEntry carries different obis representations as attributes
    list_entry = obis_values[0]
    print(list_entry.obis)            # 0100010800ff
    print(list_entry.obis.obis_code)  # 1-0:1.8.0*255
    print(list_entry.obis.obis_short) # 1.8.0

################################ MAIN #################################
def main():
    global verbose
    error = None

    parser = argparse.ArgumentParser(description='Read Landis+Gyr E320 electric power meter')
    parser.add_argument('-d', '--device', '--port', required=True, help='name of serial port device, e.g. /dev/ttyUSB0')
    parser.add_argument('-v', '--verbose', action='count', help='verbosity level')
    args = parser.parse_args()

    if args.verbose:
        verbose = args.verbose

    ser = open_serial(args.device)
    if ser:
        smlframe = read_sml(ser)        
        if smlframe:      
            dump("SMLTransportMessage", smlframe, verbose >=1)
            dosml(smlframe)
        else:
            error = "ERR_MESG"
    else:
        error = "ERR_DEVICE"

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")

    if error:
        print(now, error)

if __name__ == "__main__":
    main()
