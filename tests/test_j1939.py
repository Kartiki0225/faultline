"""Tests for the protocol layer. The expected bytes are worked out by hand from the J1939 rules."""

import j1939

EEC1 = j1939.MESSAGES[61444]
ET1 = j1939.MESSAGES[65262]


def test_engine_speed_message_has_the_well_known_identifier():
    # Priority 3, PGN 0xF004, source address 0 gives 0x0CF00400 on every J1939 engine.
    assert j1939.build_id(EEC1.priority, EEC1.pgn, j1939.ENGINE_ADDRESS) == 0x0CF00400


def test_identifier_is_split_into_priority_pgn_and_source():
    frame_id = j1939.parse_id(0x18FEEE00)
    assert (frame_id.priority, frame_id.pgn, frame_id.source) == (6, 65262, 0)
    assert frame_id.destination == j1939.GLOBAL_ADDRESS


def test_destination_is_not_part_of_the_pgn_for_addressed_messages():
    # 0xEC is below 240, so the next byte (0xFF) is the destination, not part of the PGN.
    frame_id = j1939.parse_id(0x18ECFF00)
    assert frame_id.pgn == j1939.TP_CM_PGN
    assert frame_id.destination == 0xFF
    assert j1939.build_id(6, j1939.TP_CM_PGN, 0x00) == 0x18ECFF00


def test_engine_speed_is_packed_low_byte_first_at_an_eighth_of_an_rpm():
    data = j1939.encode(EEC1, {"rpm": 1500})
    # 1500 rpm / 0.125 = 12000 = 0x2EE0, stored in bytes 4 and 5 as E0 2E.
    assert data[3:5] == bytes([0xE0, 0x2E])


def test_coolant_temperature_uses_an_offset_of_minus_40():
    assert j1939.encode(ET1, {"coolant_temp": 92})[0] == 132
    assert j1939.decode(ET1, bytes([132] + [0xFF] * 7))["coolant_temp"] == 92


def test_values_survive_an_encode_and_decode_round_trip():
    values = {"torque_mode": 1, "demand_torque": 40, "actual_torque": 35, "rpm": 1234.5}
    assert j1939.decode(EEC1, j1939.encode(EEC1, values)) == values


def test_a_small_signal_does_not_disturb_its_neighbours():
    # Torque mode is only the low 4 bits of byte 1. The high 4 bits must stay 1.
    data = j1939.encode(EEC1, {"torque_mode": 8, "rpm": 800})
    assert data[0] == 0xF8


def test_unused_bytes_are_filled_with_ff():
    assert j1939.encode(ET1, {"coolant_temp": 80})[1:] == b"\xff" * 7


def test_missing_and_faulty_values_use_the_reserved_codes():
    assert j1939.encode(ET1, {})[0] == 0xFF
    assert j1939.encode(ET1, {"coolant_temp": j1939.ERROR})[0] == 0xFE
    assert j1939.decode(ET1, b"\xff" * 8)["coolant_temp"] is j1939.NOT_AVAILABLE
    assert j1939.decode(ET1, b"\xfe" + b"\xff" * 7)["coolant_temp"] == j1939.ERROR


def test_values_outside_the_range_are_clamped_not_wrapped():
    assert j1939.decode(ET1, j1939.encode(ET1, {"coolant_temp": 9999}))["coolant_temp"] == 210
    assert j1939.decode(ET1, j1939.encode(ET1, {"coolant_temp": -500}))["coolant_temp"] == -40


def test_fault_code_packs_spn_fmi_and_count_into_four_bytes():
    dtc = j1939.Dtc(spn=110, fmi=16, count=2)
    assert j1939.encode_dtc(dtc) == bytes([0x6E, 0x00, 0x10, 0x02])
    assert j1939.decode_dtc(j1939.encode_dtc(dtc)) == dtc


def test_fault_code_handles_a_19_bit_spn():
    dtc = j1939.Dtc(spn=520192, fmi=31, count=126)
    assert j1939.decode_dtc(j1939.encode_dtc(dtc)) == dtc


def test_dm1_with_one_fault_fits_in_a_single_frame():
    payload = j1939.encode_dm1(j1939.Lamps(amber_warning=True), [j1939.Dtc(110, 16, 2)])
    assert payload == bytes([0x04, 0xFF, 0x6E, 0x00, 0x10, 0x02, 0xFF, 0xFF])
    lamps, dtcs = j1939.decode_dm1(payload)
    assert lamps == j1939.Lamps(amber_warning=True)
    assert dtcs == [j1939.Dtc(110, 16, 2)]


def test_dm1_with_no_faults_reports_lamps_off_and_an_empty_list():
    lamps, dtcs = j1939.decode_dm1(j1939.encode_dm1(j1939.Lamps(), []))
    assert lamps == j1939.Lamps()
    assert dtcs == []


def test_long_message_is_announced_then_sent_in_seven_byte_packets():
    payload = bytes(range(10))
    announce, first, second = j1939.split_bam(j1939.DM1_PGN, payload)
    # 32 = BAM, 10 bytes, 2 packets, then PGN 65226 (0x00FECA) low byte first.
    assert announce == (j1939.TP_CM_PGN, bytes([32, 10, 0, 2, 0xFF, 0xCA, 0xFE, 0x00]))
    assert first == (j1939.TP_DT_PGN, bytes([1, 0, 1, 2, 3, 4, 5, 6]))
    assert second == (j1939.TP_DT_PGN, bytes([2, 7, 8, 9, 0xFF, 0xFF, 0xFF, 0xFF]))


def feed_all(receiver, frames):
    results = [receiver.feed(j1939.parse_id(j1939.build_id(7, pgn, 0)), data) for pgn, data in frames]
    return [result for result in results if result]


def test_receiver_rebuilds_a_long_message():
    faults = [j1939.Dtc(110, 0, 1), j1939.Dtc(111, 18, 3), j1939.Dtc(100, 1, 1)]
    payload = j1939.encode_dm1(j1939.Lamps(red_stop=True), faults)

    completed = feed_all(j1939.BamReceiver(), j1939.split_bam(j1939.DM1_PGN, payload))

    assert completed == [(j1939.DM1_PGN, payload)]
    assert j1939.decode_dm1(completed[0][1]) == (j1939.Lamps(red_stop=True), faults)


def test_receiver_drops_a_message_when_a_packet_is_lost():
    frames = j1939.split_bam(j1939.DM1_PGN, bytes(range(20)))
    del frames[1]  # lose the first data packet
    assert feed_all(j1939.BamReceiver(), frames) == []
