"""Engine-output validation contracts.

Publication safety lives in cove/output_paths.py. These tests pin the rules
that the extractor publication path depends on, in particular the difference
between "the engine named a file that is simply not there" and "the engine
named a path we refuse to touch".
"""
import os

import pytest

from cove.output_paths import (
    MissingEngineOutputError,
    OutputPathError,
    create_work_directory,
    validate_engine_output,
)


def test_a_missing_engine_output_is_classified_separately(tmp_path):
    work = create_work_directory(tmp_path)
    missing = work.path / "video.mp4"

    with pytest.raises(MissingEngineOutputError) as excinfo:
        validate_engine_output(work, missing)

    assert isinstance(excinfo.value, OutputPathError)
    assert "does not exist" in str(excinfo.value)


def test_a_malformed_engine_output_path_is_still_invalid(tmp_path):
    work = create_work_directory(tmp_path)
    malformed = str(work.path / "video\x00.mp4")

    with pytest.raises(OutputPathError) as excinfo:
        validate_engine_output(work, malformed)

    assert not isinstance(excinfo.value, MissingEngineOutputError)
    assert "Invalid engine output path" in str(excinfo.value)


def test_a_missing_path_outside_the_work_directory_is_rejected_as_outside(tmp_path):
    work = create_work_directory(tmp_path)
    outside = tmp_path / "elsewhere" / "video.mp4"

    with pytest.raises(OutputPathError) as excinfo:
        validate_engine_output(work, outside)

    assert not isinstance(excinfo.value, MissingEngineOutputError)
    assert "outside its private directory" in str(excinfo.value)


def test_an_existing_regular_file_inside_the_work_directory_validates(tmp_path):
    work = create_work_directory(tmp_path)
    output = work.path / "video.mkv"
    output.write_bytes(b"payload")

    assert validate_engine_output(work, output) == output.resolve()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_symlinked_engine_output_is_rejected(tmp_path):
    work = create_work_directory(tmp_path)
    real = tmp_path / "real.mkv"
    real.write_bytes(b"payload")
    link = work.path / "video.mkv"
    link.symlink_to(real)

    with pytest.raises(OutputPathError) as excinfo:
        validate_engine_output(work, link)

    assert not isinstance(excinfo.value, MissingEngineOutputError)
    assert "symlink" in str(excinfo.value)
