#!/usr/bin/env python3
# Copyright 2026 AccelerComm Ltd.
# All rights reserved.
# This file may contain confidential or proprietary work. This
# file is subject to the terms of the licence agreement, and
# you may only use, distribute or modify this file according
# to the terms of the licence agreement.

import json
import logging
import math
from pyexpat import errors
import sys
import os
import time
from urllib import request
import pytest
from tabulate import tabulate
from datetime import datetime
from pathlib import Path

sys.path.append(os.path.realpath('../testmac/testmac/build/lib'))
sys.path.append(os.path.realpath('../testmac/testmac/pytest/'))

from test_environment.smw200a_utils import *
from test_environment import vsg
from test_environment.vsa import CollectDataException
from test_environment.scpi_interpretation import alloc_summary, bitstream_table, evm_vs_sym_table
from test_environment.scpi_interpretation import results_summary
from conftest import is_downlink_pdu
from frame_flow import frame_flow, assert_errors
from ul_test_common import messages_by_sfn_slot
from testmac_general import testmac_config

from nr_testmac import *

##################################################################

def centre_freq(metafunc):
    if "dl_centre_freq" in metafunc.fixturenames:
        dl_centre_freq_values = metafunc.config.getoption("--dl_centre_freq")
        if dl_centre_freq_values is None:
            dl_centre_freq_values = str(testmac_config.getoption(metafunc.config, "dl_centre_freq"))
        dl_centre_freq_list = []
        dl_centre_freq_ids = []
        for dl_centre_freq_value in str(dl_centre_freq_values).split(','):
            dl_centre_freq_value = dl_centre_freq_value.strip()
            if not dl_centre_freq_value:
                continue
            try:
                parsed_dl_centre_freq = int(dl_centre_freq_value)
            except ValueError as exc:
                raise pytest.UsageError("--dl_centre_freq must be an integer or a comma separated list of integers") from exc
            dl_centre_freq_list.append(parsed_dl_centre_freq)
            dl_centre_freq_ids.append(f"dl_cf_{parsed_dl_centre_freq}")
        if not dl_centre_freq_list:
            raise pytest.UsageError("--dl_centre_freq must include at least one integer value")
        metafunc.parametrize("dl_centre_freq", dl_centre_freq_list, ids=dl_centre_freq_ids, scope="session")
    else:
        raise pytest.UsageError("Missing required fixture 'dl_centre_freq' in test function signature")

def pytest_generate_tests(metafunc):
    centre_freq(metafunc)


@pytest.fixture
def test_model_name(request, dl_bandwidth_str):
    if dl_bandwidth_str not in {"BW5", "BW10"}:
        pytest.skip(f"Tx conformance test models are only defined for 5MHz/10MHz, got {dl_bandwidth_str}")
    if dl_bandwidth_str == 'BW10':
        return 'NR-FR1-TM1_1__FDD_10MHz_30kHz'
    else:
        return 'NR-FR1-TM1_1__FDD_5MHz_15kHz'

@pytest.fixture
def basic_display_setup_scpi(test_model_name, dl_centre_freq: int, dl_bandwidth_str: str, vsa_osc_source: str, vsa_sweep: str, vsa_trigger_power_dbm: float, vsa_trigger_hold_time: float, conformance_test_version: str, dl_bandwidth: int) -> 'list[str]':
    cmds = [
            "*RST",
            "*CLS",
            ":INIT:CONT OFF",
            ":SYST:ERR:CLE:REM", # Clear/Delete All Remote Errors
            ":CALC:MARK:FUNC:POW:SEL OBW",
            f":SENS:FREQ:CENT {int(dl_centre_freq)}",
            ":SENS:POW:BWID 99PCT",
            f":SENS:POW:ACH:BAND:CHAN {int(dl_bandwidth)}",
            ":SENS:POW:ACH:PRES OBW",
            ":SENS:BAND:RES 30000",
        ]
    return cmds

@pytest.fixture
def results_out_filename():
    return "Test-Model_Test_Results_" + time.strftime("%Y%m%d") + ".txt"

def write_vsa_output_to_file(filename, csv):
    with open(filename, 'w') as fp:
        fp.write(csv)
    return results_summary(csv)

@pytest.fixture
def vsa_queries():
    vsa_queries = {
        "aobw": ["CALC:MARK:FUNC:POW:RES? AOBW", lambda x: x],
        "cobw": ["CALC:MARK:FUNC:POW:RES? COBW", lambda x: x],
    }
    return vsa_queries

@pytest.fixture
def start_commands(n_frame_repeats, dl_bandwidth, dl_centre_freq):
    return [
            f":CALC:FLIN1 {int(dl_centre_freq)}",
            f":CALC:FLIN2 {int(dl_centre_freq)-(int(dl_bandwidth)/2)}",
            f":CALC:FLIN3 {int(dl_centre_freq)+(int(dl_bandwidth)/2)}",
            ":CALC:FLIN1:STAT ON",
            ":CALC:FLIN2:STAT ON",
            ":CALC:FLIN3:STAT ON",
            ":SENS:SWE:TIME:AUTO OFF",
            f":SENS:SWE:COUN {n_frame_repeats // 2}",
            ":INIT:CONT OFF",
            ":INIT:IMM;*WAI"
        ]

@pytest.fixture
def setup_commands(basic_display_setup_scpi, start_commands):
    return list(chain.from_iterable(locals().values()))

@pytest.fixture
def test_time_string():
    return time.strftime("%Y-%m-%d %H:%M:%S")

@pytest.fixture
def conformance_test_version(request):
    nodeid = request.node.nodeid
    print(f"nodeid: {nodeid}")
    test_name = nodeid.split('::', 1)[1] if '::' in nodeid else nodeid
    print(f"test_name: {test_name}")
    parts = test_name.split('_')
    print(f"parts: {parts}")
    try:
        if parts[5:8] == ['6', '5', '3']:
            return '6.5.3'
        if parts[5:8] == ['6', '3', '3']:
            return '6.3.3'
        if parts[5:8] == ['6', '6', '2']:
            return '6.6.2'
        if parts[5:8] == ['6', '6', '3']:
            return '6.6.3'
        if parts[5:8] == ['6', '6', '4']:
            return '6.6.4'
        if parts[5:7] == ['6', '2']:
            return '6.2'
        if parts[5:7] == ['16', '15']:
            return '16.15'
    except IndexError:
        pass
    print(parts[3:4])
    raise ValueError(f"Cannot determine conformance test version from test name: {test_name}")

@pytest.fixture
def test_out_bw_string(dl_bandwidth):
    dl_bandwidth_val = int(dl_bandwidth)
    if dl_bandwidth_val == 5000000:
        return "5MHz"
    elif dl_bandwidth_val == 10000000:
        return "10MHz"
    return f"{dl_bandwidth_val / 1e6:g}MHz"

"""
Generic test to load fapi-test-scenarios vectors and run them 
for a definite number of frames. Supports running a duplex frame and
multiple PDUs of any channels.
"""
def test_tx(
        test_vector,
        test_logger: logging.Logger,
        tx_messages_phy,
        vsa_queries,
        p5_initialized_pnf,
        ready_mac,
        p7_completed_node_sync,
        configured_vsa_unified, # configure the VSA after the VSG, otherwise some interference generated by the VSG will prematurely trigger the VSA
        ul_slots_ahead, dl_slots_ahead,
        vsa_option,
        skip_vsa,
        results_out_filename,
        test_time_string,
        request,
        test_out_bw_string,
        dl_centre_freq,
        test_model_name,
        dl_bandwidth,
        start_commands,
        numerology):

    test_logger.info(f"ul_slots_ahead: {ul_slots_ahead}")
    test_logger.info(f"dl_slots_ahead: {dl_slots_ahead}")


    for cmd in start_commands:
        configured_vsa_unified.write(cmd)

    rx_msgs, start_sfn = frame_flow(test_logger, ready_mac, tx_messages_phy, ul_slots_ahead, dl_slots_ahead)

    ### Checking DL result ###
    if vsa_option != "dummyvsa" and not skip_vsa:
        try:
            test_results = configured_vsa_unified.collect_data(vsa_queries)
        except CollectDataException as e:
            # Reset Scpi Device when timeout occurred.
            # When Arecibo (acl-nr-pnf) crashes, VSA gets stuck and does not allow other tests to run. This will fix it
            configured_vsa_unified.reset_connection()
            test_logger.error(f"Collect Data Error: {str(e)}")
            raise
        dl_errors = []

        for key, result in test_results.items():
            tx_conf_result = 'PASS'

            test_logger.info(f"Processing test results for conformance test version 6.6.2")
            test_logger.info(f"Raw test results: {test_results}")
            aobw_values = test_results["aobw"].split(',')
            cobw_values = test_results["cobw"].split(',')
            obw = float(aobw_values[0])
            centroid_freq = float(cobw_values[0])
            #channel_power = float(aobw_values[2])
            offset_freq1 = float(aobw_values[1])
            offset_power1 = float(aobw_values[2])
            offset_freq2 = float(aobw_values[3])
            offset_power2 = float(aobw_values[4])
            occ_bw = (obw/int(dl_bandwidth)) * 100
            occ_bw_limit = 100.0
            if not(occ_bw <= occ_bw_limit):
                dl_errors.append(
                    f"Test 6.6.2 Occupied Bandwidth: Measured Occupied bandwidth {occ_bw:.2f}% exceeded limit of {occ_bw_limit:.2f}%"
                )
                tx_conf_result = "FAIL"
            results_summary = {
                "Occupied Bandwidth (MHz)": [obw / 1e6],
                "Occupied Bandwidth (%)": [occ_bw],
                "Requirement (%)": [occ_bw_limit],
                "Centroid Frequency (MHz)": [centroid_freq / 1e6],
                #"Channel Power (dBm)": [channel_power],
                "Offset Freq Lower (MHz)": [offset_freq1 / 1e6],
                "Offset Power Lower (dBm)": [offset_power1],
                "Offset Freq Higher (MHz)": [offset_freq2 / 1e6],
                "Offset Power Higher (dBm)": [offset_power2],
                "Result": [tx_conf_result],
            }
            test_logger.info(f"Test 6.6.2 Occupied Bandwidth\nBandwidth: {test_out_bw_string}\nNumerology: {numerology}\nCentre Frequency: {(int(dl_centre_freq))/1000000000} GHz\nTimestamp: {test_time_string}\nMeasured Occupied Bandwidth: {occ_bw:.2f}%\n(Limit: {occ_bw_limit:.2f}%)\nResults Summary:\n" f"{tabulate(results_summary, headers='keys', floatfmt='.2f', tablefmt="grid", stralign="right")}")
            with open(results_out_filename, 'a+') as f: f.write(f'\nTest 6.6.2 Occupied Bandwidth\nBandwidth: {test_out_bw_string}\nNumerology: {numerology}\nCentre Frequency: {(int(dl_centre_freq))/1000000000} GHz\nTimestamp: {test_time_string}\nMeasured Occupied Bandwidth: {occ_bw:.2f}%\n(Limit: {occ_bw_limit:.2f}%)\nResults Summary:\n{tabulate(results_summary, headers='keys', floatfmt='.2f', tablefmt="grid", stralign="right")}\n')
            assert tx_conf_result == 'PASS', f"Occupied Bandwidth requirement not met: Measured Occupied Bandwidth: {occ_bw:.2f}% exceeded limit of {occ_bw_limit:.2f}%"
                
        for error in dl_errors:
            test_logger.error(error)
        assert_errors(dl_errors)

