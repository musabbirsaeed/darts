"""
PyODScorer
-----

Scorer wrapped around the individual detection algorithms of PyOD.
`PyOD https://pyod.readthedocs.io/en/latest/#`_.
"""

from typing import Optional, Sequence

import numpy as np
from pyod.models.base import BaseDetector

from darts.ad.scorers.scorers import FittableAnomalyScorer
from darts.logging import get_logger, raise_if_not
from darts.timeseries import TimeSeries

logger = get_logger(__name__)


class PyODScorer(FittableAnomalyScorer):
    def __init__(
        self,
        model,
        window: Optional[int] = None,
        component_wise: bool = False,
        diff_fn="abs_diff",
    ) -> None:

        raise_if_not(
            isinstance(model, BaseDetector),
            f"model must be BaseDetector of the library PyOD, found type: {type(component_wise)}",
        )
        self.model = model

        raise_if_not(
            type(component_wise) is bool,
            f"'component_wise' must be Boolean, found type: {type(component_wise)}",
        )
        self.component_wise = component_wise

        if component_wise:
            returns_UTS = False
        else:
            returns_UTS = True

        super().__init__(returns_UTS=returns_UTS, window=window, diff_fn=diff_fn)

    def __str__(self):
        return "PyODScorer model: {}".format(self.model.__str__().split("(")[0])

    def _fit_core(self, list_series: Sequence[TimeSeries]):

        list_np_series = [series.all_values(copy=False) for series in list_series]

        if not self.component_wise:
            self.model.fit(
                np.concatenate(
                    [
                        np.array(
                            [
                                np.array(np_series[i : i + self.window])
                                for i in range(len(np_series) - self.window + 1)
                            ]
                        ).reshape(-1, self.window * len(np_series[0]))
                        for np_series in list_np_series
                    ]
                )
            )
        else:
            models = []
            for width in range(self.width_trained_on):

                model_width = self.model
                model_width.fit(
                    np.concatenate(
                        [
                            np.array(
                                [
                                    np.array(np_series[i : i + self.window, width])
                                    for i in range(len(np_series) - self.window + 1)
                                ]
                            ).reshape(-1, self.window)
                            for np_series in list_np_series
                        ]
                    )
                )
                models.append(model_width)
            self.models = models

    def _score_core(self, series: TimeSeries) -> TimeSeries:

        raise_if_not(
            self.width_trained_on == series.width,
            "Input must have the same width of the data used for training the PyODScorer model {}, found \
            width: {} and {}".format(
                self.model.__str__().split("(")[0], self.width_trained_on, series.width
            ),
        )

        np_series = series.all_values(copy=False)
        np_anomaly_score = []

        if not self.component_wise:

            np_anomaly_score.append(
                np.exp(
                    self.model.decision_function(
                        np.array(
                            [
                                np.array(np_series[i : i + self.window])
                                for i in range(len(series) - self.window + 1)
                            ]
                        ).reshape(-1, self.window * series.width)
                    )
                )
            )
        else:

            for width in range(self.width_trained_on):
                np_anomaly_score_width = self.models[width].decision_function(
                    np.array(
                        [
                            np.array(np_series[i : i + self.window, width])
                            for i in range(len(series) - self.window + 1)
                        ]
                    ).reshape(-1, self.window)
                )

                np_anomaly_score.append(np.exp(np_anomaly_score_width))

        return TimeSeries.from_times_and_values(
            series._time_index[self.window - 1 :], list(zip(*np_anomaly_score))
        )