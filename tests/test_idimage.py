"""პირადობის მოწმობის ამოჭრა ფურცლიდან და სწორი მიმართულებით დაყენება. სურათები აქვე იხატება — ნამდვილი სკანი არ არის."""
from __future__ import annotations

from PIL import Image, ImageDraw

from hrsorter import idimage
from hrsorter.preprocess import apply_rotation

DPI = 100
A4 = (827, 1169)
MRZ = "IDGEO0000000002<<<<<<<<<<<<<<<\n9001011F3610262GEO<<<<<<<<<<<8\nBERIDZE<<NINO<<<<<<<<<<<<<<<"


def sheet() -> Image.Image:
    return Image.new("L", A4, 255)


def card(img: Image.Image, left: int, top: int, w: int = 213, h: int = 337) -> tuple[int, int, int, int]:
    """ბარათი ფურცელზე გვერდულად (ვიწრო მხარე ჰორიზონტალურად), 3.37 × 2.13 დიუმი."""
    ImageDraw.Draw(img).rectangle((left, top, left + w, top + h), fill=110)
    return left, top, left + w, top + h


def covers(box, target, slack: int = 30) -> bool:
    return all(abs(a - b) <= slack for a, b in zip(box, target))


# --- ბლოკები ----------------------------------------------------------------------------------------------------
def test_two_sides_of_a_card_on_one_sheet_are_two_blocks_in_reading_order():
    img = sheet()
    back = card(img, 500, 780)
    front = card(img, 60, 800)
    got = idimage.blocks(img, DPI)
    assert len(got) == 2 and covers(got[0], front) and covers(got[1], back)


def test_part_inside_a_bigger_block_is_not_a_separate_document():
    """პასპორტის გაშლილი გვერდი ერთი ბლოკია: ფოტოიანი გვერდი მის შიგნით ცალკე არ იჭრება."""
    img = sheet()
    d = ImageDraw.Draw(img)
    d.rectangle((100, 100, 600, 500), outline=60, width=14)
    d.rectangle((220, 200, 420, 400), fill=60)
    got = idimage.blocks(img, DPI)
    assert len(got) == 1 and covers(got[0], (100, 100, 600, 500))


def test_ordinary_text_page_is_one_block_and_a_blank_sheet_has_none():
    page = sheet()
    d = ImageDraw.Draw(page)
    for y in range(80, 1100, 18):
        d.rectangle((70, y, 760, y + 8), fill=40)
    got = idimage.blocks(page, DPI)
    assert len(got) == 1 and covers(got[0], (70, 80, 760, 1100)) and idimage.blocks(sheet(), DPI) == []


def test_specks_and_edge_shadow_are_not_documents():
    img = sheet()
    front = card(img, 60, 800)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 826, 3), fill=0)                      # სკანერის კიდის ხაზი
    for x, y in ((300, 200), (640, 330), (410, 600)):
        d.ellipse((x, y, x + 9, y + 9), fill=0)              # მტვრის წერტილები
    got = idimage.blocks(img, DPI)
    assert len(got) == 1 and covers(got[0], front)


def test_too_many_pieces_are_not_an_identity_scan():
    img = sheet()
    for i in range(5):
        card(img, 40 + 150 * i, 100 + 200 * (i % 2), 120, 170)
    assert idimage.blocks(img, DPI) == []


# --- მიმართულება ------------------------------------------------------------------------------------------------
def upright_card(text_side: bool = True) -> Image.Image:
    """სწორად დაყენებული ბარათი: ზედა მარცხენა კუთხეში მუქი ნიშანი (ყალბი მკითხველი ამით „კითხულობს“)."""
    img = Image.new("L", (337, 213), 110)
    if text_side:
        ImageDraw.Draw(img).rectangle((0, 0, 70, 70), fill=0)
    return img


def fake_read(img: Image.Image) -> str:
    return MRZ if img.width > img.height and img.getpixel((40, 40)) < 50 else "das S861 ~~"


def lying(img: Image.Image, rotation_needed: int) -> Image.Image:
    """ბარათი ისე, როგორც ფურცელზე დევს: სწორად რომ დადგეს, `rotation_needed` გრადუსით უნდა მობრუნდეს."""
    return apply_rotation(img, (360 - rotation_needed) % 360)


def test_score_counts_machine_lines_dates_and_field_words():
    assert idimage.upright_score(MRZ) == 3 * 6 + 30 and idimage.upright_score("das S861 ~~") == 0 and idimage.upright_score("") == 0
    assert idimage.upright_score("DATE OF BIRTH 01.01.1990\nPERSONAL No 01001000001") == 2 + 2 + 3


def test_broken_machine_line_still_tells_upright_from_upside_down():
    """ჭრელფონიან ბარათზე ადგილობრივი OCR ზოლს ნაწყვეტებად კითხულობს; თავდაყირა ზოლის „<“ კი „>“-ად იკითხება."""
    assert idimage.upright_score("ce BERIDZECCNINO.<<<<£<0<<082°°") == 7
    assert idimage.upright_score(">d>5>>>9>5>>22T TW\n>>>>100000100ES09209 < 1") == 0
    assert idimage.upright_score('epee 69 <M «SC\n. ite os" < — BU') == 0


def test_block_is_turned_to_the_side_where_it_reads():
    for need in idimage.ROTATIONS:
        assert idimage.rotations([lying(upright_card(), need)], fake_read) == [need]


def test_side_without_readable_text_follows_the_other_side_of_the_sheet():
    front, back = lying(upright_card(text_side=False), 90), lying(upright_card(), 90)
    assert idimage.rotations([front, back], fake_read) == [90, 90]


def test_nothing_readable_or_no_local_reader_leaves_the_image_as_is():
    blank = lying(upright_card(text_side=False), 90)
    assert idimage.rotations([blank, blank], fake_read) == [0, 0]
    assert idimage.rotations([lying(upright_card(), 180)], None) == [0]


def test_failing_local_reader_does_not_stop_the_work():
    def broken(img):
        raise OSError("tesseract is gone")
    assert idimage.rotations([upright_card()], broken) == [0]


# --- მთლიანად ---------------------------------------------------------------------------------------------------
def test_small_block_is_enlarged_for_the_reader_and_a_big_one_is_left():
    assert max(idimage.enlarge(Image.new("L", (1011, 640), 255)).size) == idimage.READER_LONG_SIDE
    big = Image.new("L", (2256, 1632), 255)
    assert idimage.enlarge(big) is big


def test_sheet_becomes_upright_enlarged_cards():
    img = sheet()
    img.paste(lying(upright_card(), 90), (60, 800))
    img.paste(lying(upright_card(), 90), (500, 780))
    got = idimage.document_images(img, DPI, fake_read)
    assert len(got) == 2
    for im in got:
        assert im.width > im.height and max(im.size) == idimage.READER_LONG_SIDE
        assert im.getpixel((round(im.width * 0.12), round(im.height * 0.2))) < 50     # ნიშანი ისევ ზედა მარცხენა კუთხეშია


def test_sheet_without_separate_blocks_is_read_whole():
    got = idimage.document_images(sheet(), DPI, None)
    assert len(got) == 1 and got[0].height > got[0].width and max(got[0].size) == idimage.READER_LONG_SIDE
