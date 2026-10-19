"""What a mission's names and paths say about it (PLAN.md §4.9, 0047).

Pure rules, so each is pinned on the names production actually has (read
2026-10-18): the port from the ``D`` suffix, the deployment from an e4edm
dataset folder, and a purpose inferred for review -- never one guessed when two
rules disagree.
"""

import pytest

from fishsense_services_api.mission_facts import (
    dataset_of,
    device_label_in_path,
    infer_purpose,
    port_from_device_label,
    strip_port_suffix,
)

# --- the port: only the D suffix says anything --------------------------------


@pytest.mark.parametrize("label", ["FSL-10D", "FSL-01D", "fsl-02d", " FSL-11D "])
def test_a_d_suffix_is_the_corrective_optic(label):
    assert port_from_device_label(label) == "corrective_optic"


@pytest.mark.parametrize(
    "label", ["FSL-10", "FSL-01", "", None, "Dad", "FSL-D", "Dive 2D", "3D"]
)
def test_a_missing_d_says_nothing(label):
    # The owner's rule: a missing D does not mean flat. Nothing is known.
    assert port_from_device_label(label) is None


@pytest.mark.parametrize(
    "path, label",
    [
        ("/fishsense_data/2025.01.17.FishSense.San Diego/ED-00/FSL-10D/Box", "FSL-10D"),
        # The dataset's location part, as production's 2023.08.03 folder has it.
        ("2023.08.03.FishSense.FSL-01D/H slate dive 4", "FSL-01D"),
        ("2023-09-07 REEF Data Dump/080123_FSL-01 Photos", None),
        ("/d/2023.08.18.FishSense.Nathans Pool/04", None),
    ],
)
def test_a_d_labelled_device_is_found_in_a_missions_path(path, label):
    assert device_label_in_path(path) == label


def test_the_suffix_is_stripped_when_matching_a_device_name():
    assert strip_port_suffix("FSL-10D") == "FSL-10"
    assert strip_port_suffix("FSL-10") == "FSL-10"
    assert strip_port_suffix(None) is None


# --- the deployment: an e4edm dataset folder ----------------------------------


@pytest.mark.parametrize(
    "path, name, prefix",
    [
        (
            "/fishsense_data/2025.01.17.FishSense.San Diego/ED-00/FSL-10D/Box",
            "2025.01.17.FishSense.San Diego",
            "/fishsense_data/2025.01.17.FishSense.San Diego",
        ),
        (
            "/fishsense_data/__unsorted-data/2023.08.14.FishSense.Nathans Pool/"
            "ED-00/FSL-02D/LaserCalibration3 [split A fish 19:00:49-19:02:33]",
            "2023.08.14.FishSense.Nathans Pool",
            "/fishsense_data/__unsorted-data/2023.08.14.FishSense.Nathans Pool",
        ),
        (
            "2023.08.03.FishSense.FSL-01D/H slate dive 4",
            "2023.08.03.FishSense.FSL-01D",
            "2023.08.03.FishSense.FSL-01D",
        ),
    ],
)
def test_a_dataset_folder_names_the_deployment(path, name, prefix):
    assert dataset_of(path) == (name, prefix)


@pytest.mark.parametrize(
    "path",
    [
        # Date and project only: a reorganised dump, not an e4edm dataset.
        "2024.06.20.REEF/08_2023/080123_FSL-01 Photos",
        "2023-09-07 REEF Data Dump/080123_FSL-01 Photos",
        "2025-02-10 REEF Data Dump SMILE 6/062524_Alligator_FSL04/x",
        # The dataset is the mission's ancestor, never the mission itself.
        "2025.01.17.FishSense.San Diego",
    ],
)
def test_other_folders_name_no_deployment(path):
    assert dataset_of(path) is None


# --- purpose: inferred for review ---------------------------------------------


@pytest.mark.parametrize(
    "name, path, purpose",
    [
        (
            "ED-00 FSL-10D LaserCalibration",
            "/d/ED-00/FSL-10D/LaserCalibration",
            "laser_calibration",
        ),
        (
            "2023-08-14 ED-00 FSL-02D LaserCalibration2",
            "/d/LaserCalibration2",
            "laser_calibration",
        ),
        ("ED-00 FSL-11D LensCalibration", "/d/LensCalibration", "lens_calibration"),
        ("ED-00 FSL-10D Box", "/d/Box", "known_length"),
        ("ED-00 FSL-10D Ginny", "/d/Ginny", "known_length"),
        ("2023-08-14 ED-00 FSL-02D George", "/d/George", "known_length"),
        ("082929_FishModels_FSL04", "/d/082929_FishModels_FSL04", "known_length"),
        ("083123_Fish Deg Angle Tests_FSL06", "/d/x", "known_length"),
        ("083123_Fish Degree Angle Test_FSL-01", "/d/x", "known_length"),
        ("082323_Slate_FSL02", "/d/082323_Slate_FSL02", "slate"),
        ("H slate dive 4", "2023.08.03.FishSense.FSL-01D/H slate dive 4", "slate"),
        ("062524_Alligator4_FSL04", "dump/062524_Alligator4_FSL04", "survey"),
        ("Hogfish01_MolHITW_0926_080323", "x/Hogfish01_MolHITW_0926_080323", "survey"),
        ("080123_FSL-01 Photos", "dump/080123_FSL-01 Photos", "survey"),
    ],
)
def test_purpose_is_inferred_from_the_name(name, path, purpose):
    assert infer_purpose(name, path)[0] == purpose


@pytest.mark.parametrize(
    "name, path",
    [
        # Two rules match: a box and a slate.
        ("2023-08-18 Nathans Pool 04-box-slate", "/d/Nathans Pool/04-box-slate"),
        # A split made to separate fish frames out of a calibration.
        (
            "2023-08-14 ED-00 FSL-02D LaserCalibration3 fish",
            "/d/LaserCalibration3 [split A fish 19:00:49-19:02:33]",
        ),
        # Calibration sessions the rules' exact phrases miss: not surveys.
        ("Checkerboard", "/d/Checkerboard"),
        ("082929_LaserCal_FSL05", "/d/082929_LaserCal_FSL05"),
        ("Calibration", "/d/Calibration"),
        ("intrinsics", "/d/intrinsics"),
        ("FSL-05 cal 2", "/d/FSL-05 cal 2"),
        # A pool session no rule names.
        ("2023-08-18 Nathans Pool 04", "/d/2023.08.18.FishSense.Nathans Pool/04"),
        ("2023-08-18 Nathans Pool 08_18_Pool_Test_FSL-02", "/d/Pool_Test_FSL-02"),
        (None, "/d/somewhere"),
    ],
)
def test_purpose_is_left_unstated_when_the_name_does_not_decide(name, path):
    purpose, reason = infer_purpose(name, path)
    assert purpose is None
    assert reason
