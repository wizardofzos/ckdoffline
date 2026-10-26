from oracle import cckdcdsk, dasdls, dasdseq

DATASETS = ["TEST.FB80.PDS", "TEST.VB.PDS", "TEST.LOAD", "TEST.FB.SEQ", "TEST.VB.SEQ"]


def test_dasdls_lists_fixture_datasets(fixture_images):
    assert set(fixture_images) == {"ckd", "cckd_zlib", "cckd_bz2", "cckd64"}
    for key, img in fixture_images.items():
        out = dasdls(img)
        for dsn in DATASETS:
            assert dsn in out, (key, dsn)
        if key != "ckd":
            assert cckdcdsk(img)[0] == 0, key


def test_oracle_reads_sequential_data(fixture_images):
    for img in fixture_images.values():
        data = dasdseq(img, "TEST.FB.SEQ")
        assert len(data) == 500 * 80
        lines = [data[i:i + 80].decode("cp037") for i in range(0, len(data), 80)]
        assert lines[0].startswith("LINE 00001 THE QUICK BROWN FOX A")
