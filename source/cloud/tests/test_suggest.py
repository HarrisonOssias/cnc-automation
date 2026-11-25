from backend.suggest import part_number_from, suggest_part

PARTS = [
    {"part_number": "BRKT-303", "material": "303 SUS", "tools": [99, 120], "files": ["bracket.step", "bracket.pdf"]},
    {"part_number": "MNT-6061", "material": "6061-T6 AL", "tools": [99, 15], "files": ["mount.step"]},
    {"part_number": "HSG-17-4", "material": "17-4 PH SS", "tools": [99, 90], "files": ["housing.step"]},
]


def test_part_number_in_file_name_wins():
    s = suggest_part("BRKT-303_revB.step", PARTS)
    assert (s["match"], s["part_number"], s["material"], s["tools"]) == (True, "BRKT-303", "303 SUS", [99, 120])
    assert s["score"] == 1.0


def test_same_file_name_as_an_earlier_upload():
    s = suggest_part("Housing.STEP", PARTS)
    assert s["match"] and s["part_number"] == "HSG-17-4" and s["score"] == 0.9


def test_similar_file_name():
    s = suggest_part("mount_v2.stp", PARTS)
    assert s["match"] and s["part_number"] == "MNT-6061"


def test_new_part_takes_its_number_from_the_file_name():
    s = suggest_part("flange plate_rev C.step", PARTS)
    assert not s["match"]
    assert (s["part_number"], s["material"], s["tools"]) == ("FLANGE-PLATE", "", [])


def test_new_part_picks_up_a_known_material_in_the_name():
    s = suggest_part("spacer-6061-T6 AL.step", PARTS)
    assert not s["match"] and s["material"] == "6061-T6 AL"


def test_no_parts_yet():
    assert suggest_part("gear.step", [])["part_number"] == "GEAR"


def test_part_number_from_drops_revision_markers():
    assert part_number_from("bracket_revB") == "BRACKET"
    assert part_number_from("mount v2") == "MOUNT"
    assert part_number_from("HSG 17-4") == "HSG-17-4"
