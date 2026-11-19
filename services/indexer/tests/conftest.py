from __future__ import annotations

from collections.abc import Callable
from datetime import date

import pytest
from pydicom.dataset import Dataset

from scoutoss_indexer.synthetic import FakePatient, SeriesSpec, StudySpec, make_instance


@pytest.fixture
def make_ds() -> Callable[..., Dataset]:
    def _make(**overrides: object) -> Dataset:
        study = StudySpec(
            FakePatient("Doe^John", "MRN100001", "M", date(1950, 3, 14)),
            date(2022, 1, 10),
            "CT CHEST W/O CONTRAST",
            [SeriesSpec("CT", "Axial Lung", "CHEST", 1)],
        )
        ds = make_instance(study, study.series[0], 0, size=8)
        for key, value in overrides.items():
            if value is None:
                if key in ds:
                    delattr(ds, key)
            else:
                setattr(ds, key, value)
        return ds

    return _make
