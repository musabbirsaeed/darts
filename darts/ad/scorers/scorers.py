"""
Scorers Base Classes
--------------------

Anomaly scorers can be trainable (FittableAnomalyScorer) or not trainable (NonFittableAnomalyScorer).

The scorers have the following main functions:
    - ``score_from_prediction()``
        Takes as input two (sequence of) series and returns the anomaly score of each pairwise element.
        An anomaly score is a series that represents how anomalous the considered point (if window = 1)
        or past W points are (if window is equal to W). The higher the value, the more anomalous the sample.
        The interpretability of the score is dependent on the scorer.

    - ``eval_accuracy_from_prediction()``
        Takes as input two (sequence of) series, computes the anomaly score of each pairwise element, and
        returns the score of an agnostic threshold metric (AUC-ROC or AUC-PR) compared to the ground truth
        of anomalies. The returned value is between 0 and 1. 1 indicates that the scorer could perfectly
        separate the anomalous point from the normal ones.

The trainable scorers have the following additional functions:
    - ``fit_from_prediction()``
        Takes two (sequence of) series as input and fits the scorer. This task is dependent on the scorer,
        but as a general case the scorer will calibrate its scoring function based on the training series that is
        considered to be anomaly-free. This training phase will allow the scorer to detect anomalies during
        the scoring phase, by comparing the series to score with the anomaly-free series seen during training.

For the trainable scorers, the previous three functions expect a tuple of (sequence of) series as input. A
function is used to compute a "difference" between the prediction series and the observation series,
in order to obtain a single "difference" series. The trainable scorer is then
applied on this series. The function is by default the absolute difference, but it can be changed thanks to
the parameter named ``diff_fn``.

It is possible to apply the trainable scorer directly on a series. This is allowed by the three following
functions. They are equivalent to the ones described previously but take as input only one (sequence of) series:
    - ``score()``
    - ``eval_accuracy()``
    - ``fit()``

As an example, the ``KMeansScorer``, which is a ``FittableAnomalyScorer``, can be applied thanks to the functions:
    - ``fit()`` and ``score()``: directly on a series to uncover the relationship between the different
    dimensions (over timesteps within windows and/or over dimensions of multivariate series).
    - ``fit_from_prediction`` and ``score_from_prediction``: which will compute a difference (residuals)
    between some prediction (coming e.g., from a forecasting model) and the series itself.
    The scorer will then flag residuals that are distant from the clusters found during the training phase.

Most of the scorers have the following main parameters:
    - `window`:
        Integer value indicating the size of the window W used by the scorer to transform the series into
        an anomaly score. A scorer will slice the given series into subsequences of size W and returns
        a value indicating how anomalous these subset of W values are. The window size must be commensurate
        to the expected durations of the anomalies one is looking for.
    - `component_wise`
        boolean parameter indicating how the scorer should behave with multivariate inputs series. If set to
        True, the model will treat each series dimension independently. If set to False, the model will
        consider the dimensions jointly in the considered `window` W to compute the score.

More details can be found in the API documentation of each scorer.
"""

# TODO:
#     - add stride for Scorers like Kmeans and Wasserstein
#     - add option to normalize the windows for kmeans? capture only the form and not the values.


from abc import ABC, abstractmethod
from typing import Any, Sequence, Union

import numpy as np

from darts import TimeSeries
from darts.ad.utils import (
    _check_timeseries_type,
    _intersect,
    _same_length,
    _sanity_check_2series,
    _to_list,
    eval_accuracy_from_scores,
    show_anomalies_from_scores,
)
from darts.logging import get_logger, raise_if_not

logger = get_logger(__name__)


class AnomalyScorer(ABC):
    """Base class for all anomaly scorers"""

    def __init__(self, univariate_scorer: bool, window: int) -> None:

        raise_if_not(
            type(window) is int,
            f"Parameter `window` must be an integer, found type {type(window)}.",
        )

        raise_if_not(
            window > 0,
            f"Parameter `window` must be stricly greater than 0, found size {window}.",
        )

        self.window = window

        self.univariate_scorer = univariate_scorer

    def _check_univariate_scorer(self, actual_anomalies):
        """Checks if `actual_anomalies` contains only univariate series when the scorer has the
        parameter 'univariate_scorer' set to True.

        'univariate_scorer' is:
            True -> when the function of the scorer ``score(series)`` (or, if applicable,
                ``score_from_prediction(actual_series, pred_series)``) returns a univariate
                anomaly score regardless of the input `series` (or, if applicable, `actual_series`
                and `pred_series`).
            False -> when the scorer will return a series that has the
                same width as the input (can be univariate or multivariate).
        """

        if self.univariate_scorer:
            actual_anomalies = _to_list(actual_anomalies)
            raise_if_not(
                all([isinstance(s, TimeSeries) for s in actual_anomalies]),
                "all series in `actual_anomalies` must be of type TimeSeries.",
            )

            raise_if_not(
                all([s.width == 1 for s in actual_anomalies]),
                f"Scorer {self.__str__()} will return a univariate anomaly score series (width=1)."
                + " Found a multivariate `actual_anomalies`."
                + " The evaluation of the accuracy cannot be computed between the two series.",
            )

    def _check_window_size(self, series: TimeSeries):
        """Checks if the parameter window is less or equal than the length of the given series"""

        raise_if_not(
            self.window <= len(series),
            f"Window size {self.window} is greater than the targeted series length {len(series)}, "
            + "must be lower or equal. Decrease the window size or increase the length series input"
            + " to score on.",
        )

    @property
    def _expects_probabilistic(self) -> bool:
        """Checks if the scorer expects a probabilistic prediction for its first input.
        By default, returns False. Needs to be overwritten by scorers that do expects
        probabilistic prediction.
        """
        return False

    def _assert_stochastic(self, series: TimeSeries, name_series: str):
        "Checks if the series is stochastic (number of samples is higher than one)."

        raise_if_not(
            series.is_stochastic,
            f"Scorer {self.__str__()} is expecting `{name_series}` to be a stochastic timeseries"
            + f" (number of samples must be higher than 1, found: {series.n_samples}).",
        )

    def _assert_deterministic(self, series: TimeSeries, name_series: str):
        "Checks if the series is deterministic (number of samples is equal to one)."

        if not series.is_deterministic:
            logger.warning(
                f"Scorer {self.__str__()} is expecting `{name_series}` to be a (sequence of) deterministic"
                + f" timeseries (number of samples must be equal to 1, found: {series.n_samples}). The "
                + "series will be converted to a deterministic series by taking the median of the samples.",
            )
            series = series.quantile_timeseries(quantile=0.5)

        return series

    @abstractmethod
    def __str__(self):
        "returns the name of the scorer"
        pass

    def eval_accuracy_from_prediction(
        self,
        actual_anomalies: Union[TimeSeries, Sequence[TimeSeries]],
        actual_series: Union[TimeSeries, Sequence[TimeSeries]],
        pred_series: Union[TimeSeries, Sequence[TimeSeries]],
        metric: str = "AUC_ROC",
    ) -> Union[float, Sequence[float], Sequence[Sequence[float]]]:
        """Computes the anomaly score between `actual_series` and `pred_series`, and returns the score
        of an agnostic threshold metric.

        Parameters
        ----------
        actual_anomalies
            The (sequence of) ground truth of the anomalies (1 if it is an anomaly and 0 if not)
        actual_series
            The (sequence of) actual series.
        pred_series
            The (sequence of) predicted series.
        metric
            Optionally, metric function to use. Must be one of "AUC_ROC" and "AUC_PR".
            Default: "AUC_ROC"

        Returns
        -------
        Union[float, Sequence[float], Sequence[Sequence[float]]]
            Score of an agnostic threshold metric for the computed anomaly score
                - ``float`` if `actual_series` and `actual_series` are univariate series (dimension=1).
                - ``Sequence[float]``

                    * if `actual_series` and `actual_series` are multivariate series (dimension>1),
                    returns one value per dimension, or
                    * if `actual_series` and `actual_series` are sequences of univariate series,
                    returns one value per series
                - ``Sequence[Sequence[float]]]`` if `actual_series` and `actual_series` are sequences
                of multivariate series. Outer Sequence is over the sequence input and the inner
                Sequence is over the dimensions of each element in the sequence input.
        """

        self._check_univariate_scorer(actual_anomalies)

        anomaly_score = self.score_from_prediction(actual_series, pred_series)

        return eval_accuracy_from_scores(
            actual_anomalies, anomaly_score, self.window, metric
        )

    @abstractmethod
    def score_from_prediction(self, actual_series: Any, pred_series: Any) -> Any:
        pass

    def show_anomalies_from_prediction(
        self,
        actual_series: TimeSeries,
        pred_series: TimeSeries,
        name_of_scorer: str = None,
        actual_anomalies: TimeSeries = None,
        title: str = None,
        metric: str = None,
    ):
        """Plot the results of the scorer.

        Computes the anomaly score on the two series. And plots the results.

        The plot will be composed of the following:
            - the actual_series and the pred_series.
            - the anomaly score of the scorer.
            - the actual anomalies, if given.

        It is possible to:
            - add a title to the figure with the parameter `title`
            - give personalized name to the scorer with `name_of_scorer`
            - show the results of a metric for the anomaly score (AUC_ROC or AUC_PR),
              if the actual anomalies is provided.

        Parameters
        ----------
        actual_series
            The actual series to visualize anomalies from.
        pred_series
            The predicted series of `actual_series`.
        actual_anomalies
            The ground truth of the anomalies (1 if it is an anomaly and 0 if not)
        name_of_scorer
            Name of the scorer.
        title
            Title of the figure
        metric
            Optionally, Scoring function to use. Must be one of "AUC_ROC" and "AUC_PR".
            Default: "AUC_ROC"
        """
        if isinstance(actual_series, Sequence):
            raise_if_not(
                len(actual_series) == 1,
                "``show_anomalies_from_prediction`` expects only one series for `actual_series`,"
                + f" found a list of length {len(actual_series)} as input.",
            )

            actual_series = actual_series[0]

        raise_if_not(
            isinstance(actual_series, TimeSeries),
            "``show_anomalies_from_prediction`` expects an input of type TimeSeries,"
            + f" found type {type(actual_series)} for `actual_series`.",
        )

        if isinstance(pred_series, Sequence):
            raise_if_not(
                len(pred_series) == 1,
                "``show_anomalies_from_prediction`` expects one series for `pred_series`,"
                + f" found a list of length {len(pred_series)} as input.",
            )

            pred_series = pred_series[0]

        raise_if_not(
            isinstance(pred_series, TimeSeries),
            "``show_anomalies_from_prediction`` expects an input of type TimeSeries,"
            + f" found type: {type(pred_series)} for `pred_series`.",
        )

        anomaly_score = self.score_from_prediction(actual_series, pred_series)

        if title is None:
            title = f"Anomaly results by scorer {self.__str__()}"

        if name_of_scorer is None:
            name_of_scorer = [f"anomaly score by {self.__str__()}"]

        return show_anomalies_from_scores(
            actual_series,
            model_output=pred_series,
            anomaly_scores=anomaly_score,
            window=self.window,
            names_of_scorers=name_of_scorer,
            actual_anomalies=actual_anomalies,
            title=title,
            metric=metric,
        )


class NonFittableAnomalyScorer(AnomalyScorer):
    """Base class of anomaly scorers that do not need training."""

    def __init__(self, univariate_scorer, window) -> None:
        super().__init__(univariate_scorer=univariate_scorer, window=window)

        # indicates if the scorer is trainable or not
        self.trainable = False

    @abstractmethod
    def _score_core_from_prediction(self, series: Any) -> Any:
        pass

    def score_from_prediction(
        self,
        actual_series: Union[TimeSeries, Sequence[TimeSeries]],
        pred_series: Union[TimeSeries, Sequence[TimeSeries]],
    ) -> Union[TimeSeries, Sequence[TimeSeries]]:
        """Computes the anomaly score on the two (sequence of) series.

        If a pair of sequences is given, they must contain the same number
        of series. The scorer will score each pair of series independently
        and return an anomaly score for each pair.

        Parameters
        ----------
        actual_series:
            The (sequence of) actual series.
        pred_series
            The (sequence of) predicted series.

        Returns
        -------
        Union[TimeSeries, Sequence[TimeSeries]]
            (Sequence of) anomaly score time series
        """
        list_actual_series, list_pred_series = _to_list(actual_series), _to_list(
            pred_series
        )
        _same_length(list_actual_series, list_pred_series)

        anomaly_scores = []

        for s1, s2 in zip(list_actual_series, list_pred_series):
            _sanity_check_2series(s1, s2)
            s1, s2 = _intersect(s1, s2)
            self._check_window_size(s1)
            self._check_window_size(s2)
            anomaly_scores.append(self._score_core_from_prediction(s1, s2))

        if (
            len(anomaly_scores) == 1
            and not isinstance(pred_series, Sequence)
            and not isinstance(actual_series, Sequence)
        ):
            return anomaly_scores[0]
        else:
            return anomaly_scores


class FittableAnomalyScorer(AnomalyScorer):
    """Base class of scorers that do need training."""

    def __init__(self, univariate_scorer, window, diff_fn="abs_diff") -> None:
        super().__init__(univariate_scorer=univariate_scorer, window=window)

        # indicates if the scorer is trainable or not
        self.trainable = True

        # indicates if the scorer has been trained yet
        self._fit_called = False

        # function used in ._diff_series() to convert 2 time series into 1
        if diff_fn in {"abs_diff", "diff"}:
            self.diff_fn = diff_fn
        else:
            raise ValueError(f"Metric should be 'diff' or 'abs_diff', found {diff_fn}")

    def check_if_fit_called(self):
        """Checks if the scorer has been fitted before calling its `score()` function."""

        raise_if_not(
            self._fit_called,
            f"The Scorer {self.__str__()} has not been fitted yet. Call ``fit()`` first.",
        )

    def eval_accuracy(
        self,
        actual_anomalies: Union[TimeSeries, Sequence[TimeSeries]],
        series: Union[TimeSeries, Sequence[TimeSeries]],
        metric: str = "AUC_ROC",
    ) -> Union[float, Sequence[float], Sequence[Sequence[float]]]:
        """Computes the anomaly score of the given time series, and returns the score
        of an agnostic threshold metric.

        Parameters
        ----------
        actual_anomalies
            The ground truth of the anomalies (1 if it is an anomaly and 0 if not)
        series
            The (sequence of) series to detect anomalies from.
        metric
            Optionally, metric function to use. Must be one of "AUC_ROC" and "AUC_PR".
            Default: "AUC_ROC"

        Returns
        -------
        Union[float, Sequence[float], Sequence[Sequence[float]]]
            Score of an agnostic threshold metric for the computed anomaly score
                - ``float`` if `series` is a univariate series (dimension=1).
                - ``Sequence[float]``

                    * if `series` is a multivariate series (dimension>1), returns one
                    value per dimension, or
                    * if `series` is a sequence of univariate series, returns one value
                    per series
                - ``Sequence[Sequence[float]]]`` if `series` is a sequence of multivariate
                series. Outer Sequence is over the sequence input and the inner Sequence
                is over the dimensions of each element in the sequence input.
        """
        self._check_univariate_scorer(actual_anomalies)
        anomaly_score = self.score(series)

        return eval_accuracy_from_scores(
            actual_anomalies, anomaly_score, self.window, metric
        )

    def score(
        self,
        series: Union[TimeSeries, Sequence[TimeSeries]],
    ) -> Union[TimeSeries, Sequence[TimeSeries]]:
        """Computes the anomaly score on the given series.

        If a sequence of series is given, the scorer will score each series independently
        and return an anomaly score for each series in the sequence.

        Parameters
        ----------
        series
            The (sequence of) series to detect anomalies from.

        Returns
        -------
        Union[TimeSeries, Sequence[TimeSeries]]
            (Sequence of) anomaly score time series
        """

        self.check_if_fit_called()

        list_series = _to_list(series)

        anomaly_scores = []
        for s in list_series:
            _check_timeseries_type(s)
            self._check_window_size(s)
            anomaly_scores.append(
                self._score_core(self._assert_deterministic(s, "series"))
            )

        if len(anomaly_scores) == 1 and not isinstance(series, Sequence):
            return anomaly_scores[0]
        else:
            return anomaly_scores

    def show_anomalies(
        self,
        series: TimeSeries,
        actual_anomalies: TimeSeries = None,
        name_of_scorer: str = None,
        title: str = None,
        metric: str = None,
    ):
        """Plot the results of the scorer.

        Computes the score on the given series input. And plots the results.

        The plot will be composed of the following:
            - the series itself.
            - the anomaly score of the score.
            - the actual anomalies, if given.

        It is possible to:
            - add a title to the figure with the parameter `title`
            - give personalized name to the scorer with `name_of_scorer`
            - show the results of a metric for the anomaly score (AUC_ROC or AUC_PR),
            if the actual anomalies is provided.

        Parameters
        ----------
        series
            The series to visualize anomalies from.
        actual_anomalies
            The ground truth of the anomalies (1 if it is an anomaly and 0 if not)
        name_of_scorer
            Name of the scorer.
        title
            Title of the figure
        metric
            Optionally, Scoring function to use. Must be one of "AUC_ROC" and "AUC_PR".
            Default: "AUC_ROC"
        """

        if isinstance(series, Sequence):
            raise_if_not(
                len(series) == 1,
                "``show_anomalies`` expects one series for `series`,"
                + f" found a list of length {len(series)} as input.",
            )

            series = series[0]

        raise_if_not(
            isinstance(series, TimeSeries),
            "``show_anomalies`` expects an input of type TimeSeries,"
            + f" found type {type(series)} for `series`.",
        )

        anomaly_score = self.score(series)

        if title is None:
            title = f"Anomaly results by scorer {self.__str__()}"

        if name_of_scorer is None:
            name_of_scorer = f"anomaly score by {self.__str__()}"

        return show_anomalies_from_scores(
            series,
            anomaly_scores=anomaly_score,
            window=self.window,
            names_of_scorers=name_of_scorer,
            actual_anomalies=actual_anomalies,
            title=title,
            metric=metric,
        )

    def score_from_prediction(
        self,
        actual_series: Union[TimeSeries, Sequence[TimeSeries]],
        pred_series: Union[TimeSeries, Sequence[TimeSeries]],
    ) -> Union[TimeSeries, Sequence[TimeSeries]]:
        """Computes the anomaly score on the two (sequence of) series.

        The function ``diff_fn`` passed as a parameter to the scorer, will transform `pred_series` and `actual_series`
        into one "difference" series. By default, ``diff_fn`` will compute the absolute difference
        (Default: "abs_diff").
        If actual_series and pred_seriesare sequences, ``diff_fn`` will be applied to all pairwise elements
        of the sequences.

        The scorer will then transform this series into an anomaly score. If a sequence of series is given,
        the scorer will score each series independently and return an anomaly score for each series in the sequence.

        Parameters
        ----------
        actual_series
            The (sequence of) actual series.
        pred_series
            The (sequence of) predicted series.

        Returns
        -------
        Union[TimeSeries, Sequence[TimeSeries]]
            (Sequence of) anomaly score time series
        """

        self.check_if_fit_called()

        list_actual_series, list_pred_series = _to_list(actual_series), _to_list(
            pred_series
        )
        _same_length(list_actual_series, list_pred_series)

        anomaly_scores = []
        for (s1, s2) in zip(list_actual_series, list_pred_series):
            _sanity_check_2series(s1, s2)
            s1 = self._assert_deterministic(s1, "actual_series")
            s2 = self._assert_deterministic(s2, "pred_series")
            diff = self._diff_series(s1, s2)
            self._check_window_size(diff)
            anomaly_scores.append(self.score(diff))

        if (
            len(anomaly_scores) == 1
            and not isinstance(pred_series, Sequence)
            and not isinstance(actual_series, Sequence)
        ):
            return anomaly_scores[0]
        else:
            return anomaly_scores

    def fit(
        self,
        series: Union[TimeSeries, Sequence[TimeSeries]],
    ):
        """Fits the scorer on the given time series input.

        If sequence of series is given, the scorer will be fitted on the concatenation of the sequence.

        The assumptions is that the series `series` used for training are generally anomaly-free.

        Parameters
        ----------
        series
            The (sequence of) series with no anomalies.

        Returns
        -------
        self
            Fitted Scorer.
        """
        list_series = _to_list(series)

        for idx, s in enumerate(list_series):
            _check_timeseries_type(s)

            if idx == 0:
                self.width_trained_on = s.width
            else:
                raise_if_not(
                    s.width == self.width_trained_on,
                    "series in `series` must have the same width,"
                    + f" found width {self.width_trained_on}"
                    + f" and {s.width} for index 0 and {idx}.",
                )
            self._check_window_size(s)

            self._assert_deterministic(s, "series")

        self._fit_core(list_series)
        self._fit_called = True

    def fit_from_prediction(
        self,
        actual_series: Union[TimeSeries, Sequence[TimeSeries]],
        pred_series: Union[TimeSeries, Sequence[TimeSeries]],
    ):
        """Fits the scorer on the two (sequence of) series.

        The function ``diff_fn`` passed as a parameter to the scorer, will transform `pred_series` and `actual_series`
        into one series. By default, ``diff_fn`` will compute the absolute difference (Default: "abs_diff").
        If `pred_series` and `actual_series` are sequences, ``diff_fn`` will be applied to all pairwise elements
        of the sequences.

        The scorer will then be fitted on this (sequence of) series. If a sequence of series is given,
        the scorer will be fitted on the concatenation of the sequence.

        The scorer assumes that the (sequence of) actual_series is anomaly-free.

        Parameters
        ----------
        actual_series
            The (sequence of) actual series.
        pred_series
            The (sequence of) predicted series.

        Returns
        -------
        self
            Fitted Scorer.
        """
        list_actual_series, list_pred_series = _to_list(actual_series), _to_list(
            pred_series
        )
        _same_length(list_actual_series, list_pred_series)

        list_fit_series = []
        for s1, s2 in zip(list_actual_series, list_pred_series):
            _sanity_check_2series(s1, s2)
            s1 = self._assert_deterministic(s1, "actual_series")
            s2 = self._assert_deterministic(s2, "pred_series")
            list_fit_series.append(self._diff_series(s1, s2))

        self.fit(list_fit_series)
        self._fit_called = True

    @abstractmethod
    def _fit_core(self, series: Any) -> Any:
        pass

    @abstractmethod
    def _score_core(self, series: Any) -> Any:
        pass

    def _diff_sequence(
        self, list_series_1: Sequence[TimeSeries], list_series_2: Sequence[TimeSeries]
    ) -> Sequence[TimeSeries]:
        """Calls the function ``_diff_series()`` on every pair (s1,s2) in the list (list_series_1,list_series_2).

        `list_series_1` and `list_series_2` must have the same length n. Each pair of series in `list_series_1` and
        `list_series_2` must be of the same length and width/dimension.

        Parameters
        ----------
        series_1
            1st sequence of time series
        series_2:
            2nd sequence of time series

        Returns
        -------
        TimeSeries
            Sequence of series of length n
        """

        return list(map(self._diff_series, list_series_1, list_series_2))

    def _diff_series(self, series_1: TimeSeries, series_2: TimeSeries) -> TimeSeries:
        """Applies the ``diff_fn`` to the two time series. Converts two time series into 1.

        series_1 and series_2 must:
            - have a non empty time intersection
            - be of the same width/dimension W

        Parameters
        ----------
        series_1
            1st time series
        series_2:
            2nd time series

        Returns
        -------
        TimeSeries
            series of width/dimension W
        """
        series_1, series_2 = _intersect(series_1, series_2)

        if self.diff_fn == "abs_diff":
            return (series_1 - series_2).map(lambda x: np.abs(x))
        elif self.diff_fn == "diff":
            return series_1 - series_2
        else:
            # found an non-existent diff_fn
            raise ValueError(
                f"Metric should be 'diff' or 'abs_diff', found {self.diff_fn}"
            )


class NLLScorer(NonFittableAnomalyScorer):
    """Parent class for all LikelihoodScorer"""

    def __init__(self, window) -> None:
        super().__init__(univariate_scorer=False, window=window)

    def _score_core_from_prediction(
        self,
        actual_series: TimeSeries,
        pred_series: TimeSeries,
    ) -> TimeSeries:
        """For each timestamp of the inputs:
            - the parameters of the considered distribution are fitted on the samples of the probabilistic time series
            - the negative log-likelihood of the determinisitc time series values are computed

        The score will be computed on each component independently. Additionally, if the series is
        multivariate, the score will be computed on each width independently.

        Parameters
        ----------
        actual_series:
            A determinisict time series (number of samples per timestamp must be equal to 1)
        pred_series
            A probabilistic time series (number of samples per timestamp must be higher than 1)

        Returns
        -------
        TimeSeries
        """
        actual_series = self._assert_deterministic(actual_series, "actual_series")
        self._assert_stochastic(pred_series, "pred_series")

        np_actual_series = actual_series.all_values(copy=False)
        np_pred_series = pred_series.all_values(copy=False)

        np_anomaly_scores = []
        for width in range(pred_series.width):
            np_anomaly_scores.append(
                self._score_core_nllikelihood(
                    # shape actual: (time_steps, )
                    # shape pred: (time_steps, samples)
                    np_actual_series[:, width].flatten(),
                    np_pred_series[:, width],
                )
            )

        anomaly_scores = TimeSeries.from_times_and_values(
            pred_series.time_index, list(zip(*np_anomaly_scores))
        )

        return self.window_adjustment_series(anomaly_scores)

    def window_adjustment_series(
        self,
        series: TimeSeries,
    ) -> TimeSeries:
        """Slides a window of size self.window along the input series, and replaces the value of
        the input time series by the mean of the values contained in the window (past self.window
        points, including itself).

        A series of length N will be transformed into a series of length N-self.window+1.

        Parameters
        ----------
        series
            time series to adjust

        Returns
        -------
            TimeSeries
        """

        if self.window == 1:
            # the process results in replacing every value by itself -> return directly the series
            return series
        else:
            return series.window_transform(
                transforms={
                    "window": self.window,
                    "function": "mean",
                    "mode": "rolling",
                    "min_periods": self.window,
                },
                treat_na="dropna",
            )

    def _expects_probabilistic(self) -> bool:
        return True

    @abstractmethod
    def _score_core_nllikelihood(self, input_1: Any, input_2: Any) -> Any:
        """For each timestamp, the corresponding distribution is fitted on the probabilistic time-series
        input_2, and returns the negative log-likelihood of the deterministic time-series input_1
        given the distribution.
        """
        pass
