import shutil
import subprocess
import zipfile

import pytest

import core
from providers import apple

CSV = (
    '"Data_Type","File_Name","File_Link","File_Type","GPG_Size","GPG_SHA256"\n'
    '"Account","a.zip.gpg","https://example.com/a","x","1","' + "ab" * 32 + '"\n'
)


def _make_zip(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Account_Data_Links/x-account-download-details.csv", CSV)
        zf.writestr("Account_Data_Links/readme.txt", "oi")


def test_zip_package(tmp_path):
    z = tmp_path / "p.zip"
    _make_zip(z)
    entries = apple.load_entries(z)
    assert [e.file_name for e in entries] == ["a.zip.gpg"]


@pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg ausente")
def test_gpg_package(tmp_path):
    z = tmp_path / "p.zip"
    _make_zip(z)
    gpg = tmp_path / "p.zip.gpg"
    subprocess.run(
        ["gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "segredo",
         "--symmetric", "--output", str(gpg), str(z)],
        check=True, capture_output=True, env={"GNUPGHOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    with pytest.raises(core.PipelineError):
        apple.load_entries(gpg, "")
    with pytest.raises(core.PipelineError):
        apple.load_entries(gpg, "errada")
    assert apple.load_entries(gpg, "segredo")[0].expected_hash == "ab" * 32


def test_zip_without_csv(tmp_path):
    z = tmp_path / "p.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("a.txt", "x")
    with pytest.raises(core.PipelineError):
        apple.load_entries(z)
