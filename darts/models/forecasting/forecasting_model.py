"""
Forecasting Model Base Classes

A forecasting model captures the future values of a time series as a function of the past as follows:

.. math:: y_{t+1} = f(y_t, y_{t-1}, ..., y_1),

where :math:`y_t` represents the time series' value(s) at time :math:`t`.

The main functions are `fit()` and `predict()`. `fit()` learns the function `f()`, over the history of
one or several time series. The function `predict()` applies `f()` on one or several time series in order
to obtain forecasts for a desired number of time stamps into the future.
"""
import copy
import datetime
import inspect
import os
import pickle
import time
from abc import ABC, ABCMeta, abstractmethod
from collections import OrderedDict
from itertools import product
from random import sample
from typing import Any, BinaryIO, Callable, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

from darts import metrics
from darts.dataprocessing.encoders import SequentialEncoder
from darts.logging import get_logger, raise_if, raise_if_not, raise_log
from darts.timeseries import TimeSeries
from darts.utils import (
    _build_tqdm_iterator,
    _historical_forecasts_general_checks,
    _parallel_apply,
    _retrain_wrapper,
    _with_sanity_checks,
)
from darts.utils.timeseries_generation import (
    _build_forecast_series,
    _generate_new_dates,
)

logger = get_logger(__name__)


class ModelMeta(ABCMeta):
    """Meta class to store parameters used at model creation.

    When creating a model instance, the parameters are extracted as follows:

        1)  Get the model's __init__ signature and store all arg and kwarg
            names as well as default values (empty for args) in an ordered
            dict `all_params`.
        2)  Replace the arg values from `all_params` with the positional
            args used at model creation.
        3)  Remove args from `all_params` that were not passed as positional
            args at model creation. This will enforce that an error is raised
            if not all positional args were passed. If all positional args
            were passed, no parameter will be removed.
        4)  Update `all_params` kwargs with optional kwargs from model creation.
        5)  Save `all_params` to the model.
        6)  Call (create) the model with `all_params`.
    """

    def __call__(cls, *args, **kwargs):
        # 1) get all default values from class' __init__ signature
        sig = inspect.signature(cls.__init__)
        all_params = OrderedDict(
            [
                (p.name, p.default)
                for p in sig.parameters.values()
                if not p.name == "self"
            ]
        )

        # 2) fill params with positional args
        for param, arg in zip(all_params, args):
            all_params[param] = arg

        # 3) remove args which were not set (and are per default empty)
        remove_params = []
        for param, val in all_params.items():
            if val is sig.parameters[param].empty:
                remove_params.append(param)
        for param in remove_params:
            all_params.pop(param)

        # 4) update defaults with actual model call parameters and store
        all_params.update(kwargs)

        # 5) save parameters in model
        cls._model_call = all_params

        # 6) call model
        return super().__call__(**all_params)


class ForecastingModel(ABC, metaclass=ModelMeta):
    """The base class for forecasting models. It defines the *minimal* behavior that all forecasting models have to
    support. The signatures in this base class are for "local" models handling only one univariate series and no
    covariates. Sub-classes can handle more complex cases.
    """

    @abstractmethod
    def __init__(self, *args, **kwargs):
        # The series used for training the model through the `fit()` function.
        # This is only used if the model has been fit on one time series.
        self.training_series: Optional[TimeSeries] = None

        # static covariates sample from the (first) target series used for training the model through the `fit()`
        # function.
        self.static_covariates: Optional[pd.DataFrame] = None

        # state; whether the model has been fit (on a single time series)
        self._fit_called = False

        # extract and store sub class model creation parameters
        self._model_params = self._extract_model_creation_params()

    @abstractmethod
    def fit(self, series: TimeSeries) -> "ForecastingModel":
        """Fit/train the model on the provided series.

        Parameters
        ----------
        series
            A target time series. The model will be trained to forecast this time series.

        Returns
        -------
        self
            Fitted model.
        """
        if not isinstance(self, FutureCovariatesLocalForecastingModel):
            series._assert_univariate()
        raise_if_not(
            len(series) >= self.min_train_series_length,
            "Train series only contains {} elements but {} model requires at least {} entries".format(
                len(series), str(self), self.min_train_series_length
            ),
        )
        self.training_series = series
        self._fit_called = True

        if series.has_range_index:
            self._supports_range_index()

    def _supports_range_index(self) -> bool:
        """Checks if the forecasting model supports a range index.
        Some models may not support this, if for instance they rely on underlying dates.

        By default, returns True. Needs to be overwritten by models that do not support
        range indexing and raise meaningful exception.
        """
        return True

    def _is_probabilistic(self) -> bool:
        """
        Checks if the forecasting model supports probabilistic predictions.
        By default, returns False. Needs to be overwritten by models that do support
        probabilistic predictions.
        """
        return False

    def _supports_non_retrainable_historical_forecasts(self) -> bool:
        """
        Checks if the forecasting model supports historical forecasts without retraining
        the model. By default, returns False. Needs to be overwritten by models that do
        support historical forecasts without retraining.
        """
        return False

    @property
    def uses_past_covariates(self):
        return "past_covariates" in inspect.signature(self.fit).parameters.keys()

    @property
    def uses_future_covariates(self):
        return "future_covariates" in inspect.signature(self.fit).parameters.keys()

    @abstractmethod
    def predict(self, n: int, num_samples: int = 1) -> TimeSeries:
        """Forecasts values for `n` time steps after the end of the training series.

        Parameters
        ----------
        n
            Forecast horizon - the number of time steps after the end of the series for which to produce predictions.
        num_samples
            Number of times a prediction is sampled from a probabilistic model. Should be left set to 1
            for deterministic models.

        Returns
        -------
        TimeSeries
            A time series containing the `n` next points after then end of the training series.
        """
        if not self._fit_called:
            raise_log(
                ValueError(
                    "The model must be fit before calling predict(). "
                    "For global models, if predict() is called without specifying a series, "
                    "the model must have been fit on a single training series."
                ),
                logger,
            )

        if not self._is_probabilistic() and num_samples > 1:
            raise_log(
                ValueError(
                    "`num_samples > 1` is only supported for probabilistic models."
                ),
                logger,
            )

    def _fit_wrapper(
        self,
        series: TimeSeries,
        past_covariates: Optional[TimeSeries],
        future_covariates: Optional[TimeSeries],
    ):
        self.fit(series)

    def _predict_wrapper(
        self,
        n: int,
        series: TimeSeries,
        past_covariates: Optional[TimeSeries],
        future_covariates: Optional[TimeSeries],
        num_samples: int,
    ) -> TimeSeries:
        return self.predict(n, num_samples=num_samples)

    @property
    def min_train_series_length(self) -> int:
        """
        Class property defining the minimum required length for the training series.
        This function/property should be overridden if a value higher than 3 is required.
        """
        return 3

    def _generate_new_dates(
        self, n: int, input_series: Optional[TimeSeries] = None
    ) -> Union[pd.DatetimeIndex, pd.RangeIndex]:
        """
        Generates `n` new dates after the end of the specified series
        """
        input_series = (
            input_series if input_series is not None else self.training_series
        )
        return _generate_new_dates(n=n, input_series=input_series)

    def _build_forecast_series(
        self,
        points_preds: Union[np.ndarray, Sequence[np.ndarray]],
        input_series: Optional[TimeSeries] = None,
    ) -> TimeSeries:
        """
        Builds a forecast time series starting after the end of the training time series, with the
        correct time index (or after the end of the input series, if specified).
        """
        input_series = (
            input_series if input_series is not None else self.training_series
        )
        return _build_forecast_series(points_preds, input_series)

    def _historical_forecasts_sanity_checks(self, *args: Any, **kwargs: Any) -> None:
        """Sanity checks for the historical_forecasts function

        Parameters
        ----------
        args
            The args parameter(s) provided to the historical_forecasts function.
        kwargs
            The kwargs parameter(s) provided to the historical_forecasts function.

        Raises
        ------
        ValueError
            when a check on the parameter does not pass.
        """
        # parse args and kwargs
        series = args[0]
        _historical_forecasts_general_checks(series, kwargs)

    def _get_last_prediction_time(self, series, forecast_horizon, overlap_end):
        if overlap_end:
            last_valid_pred_time = series.time_index[-1]
        else:
            last_valid_pred_time = series.time_index[-forecast_horizon]

        return last_valid_pred_time

    @_with_sanity_checks("_historical_forecasts_sanity_checks")
    def historical_forecasts(
        self,
        series: TimeSeries,
        past_covariates: Optional[TimeSeries] = None,
        future_covariates: Optional[TimeSeries] = None,
        num_samples: int = 1,
        train_length: Optional[int] = None,
        start: Union[pd.Timestamp, float, int] = 0.5,
        forecast_horizon: int = 1,
        stride: int = 1,
        retrain: Union[bool, int, Callable[..., bool]] = True,
        overlap_end: bool = False,
        last_points_only: bool = True,
        verbose: bool = False,
    ) -> Union[TimeSeries, List[TimeSeries]]:

        """Compute the historical forecasts that would have been obtained by this model on the `series`.

        This method uses an expanding training window;
        it repeatedly builds a training set from the beginning of `series`. It trains the
        model on the training set, emits a forecast of length equal to forecast_horizon, and then moves
        the end of the training set forward by `stride` time steps.

        By default, this method will return a single time series made up of the last point of each
        historical forecast. This time series will thus have a frequency of ``series.freq * stride``.
        If `last_points_only` is set to False, it will instead return a list of the
        historical forecasts series.

        By default, this method always re-trains the models on the entire available history,
        corresponding to an expanding window strategy.
        If `retrain` is set to False, the model will only be trained on the initial training window
        (up to `start` time stamp), and only if it has not been trained before. This is not
        supported by all models.

        Parameters
        ----------
        series
            The target time series to use to successively train and evaluate the historical forecasts.
        past_covariates
            An optional past-observed covariate series. This applies only if the model supports past covariates.
        future_covariates
            An optional future-known covariate series. This applies only if the model supports future covariates.
        num_samples
            Number of times a prediction is sampled from a probabilistic model. Should be left set to 1
            for deterministic models.
        train_length
            Number of time steps in our training set (size of backtesting window to train on).
            Default is set to train_length=None where it takes all available time steps up until prediction time,
            otherwise the moving window strategy is used. If larger than the number of time steps available, all steps
            up until prediction time are used, as in default case. Needs to be at least min_train_series_length.
        start
            The first point of time at which a prediction is computed for a future time.
            This parameter supports 3 different data types: ``float``, ``int`` and ``pandas.Timestamp``.
            In the case of ``float``, the parameter will be treated as the proportion of the time series
            that should lie before the first prediction point.
            In the case of ``int``, the parameter will be treated as an integer index to the time index of
            `series` that will be used as first prediction time.
            In case of ``pandas.Timestamp``, this time stamp will be used to determine the first prediction time
            directly.
        forecast_horizon
            The forecast horizon for the predictions
        stride
            The number of time steps between two consecutive predictions.
        retrain
            Whether and/or on which condition to retrain the model before predicting.
            This parameter supports 3 different datatypes: ``bool``, (positive) ``int``, and
            ``Callable`` (returning a ``bool``).
            In the case of ``bool``: retrain the model at each step (`True`), or never retrains the model (`False`).
            In the case of ``int``: the model is retrained every `retrain` iterations.
            In the case of ``Callable``: the model is retrained whenever callable returns `True`.
            Arguments passed to the callable are as follows:

                - `pred_time (pd.Timestamp or int)`: timestamp of forecast time (end of the training series)
                - `train_series (TimeSeries)`: train series up to `pred_time`
                - `past_covariates (TimeSeries)`: past_covariates series up to `pred_time`
                - `future_covariates (TimeSeries)`: future_covariates series up
                  to `min(pred_time + series.freq * forecast_horizon, series.end_time())`

            Note: some models do require being retrained every time
            and do not support anything else than `retrain=True`.
        overlap_end
            Whether the returned forecasts can go beyond the series' end or not
        last_points_only
            Whether to retain only the last point of each historical forecast.
            If set to True, the method returns a single ``TimeSeries`` containing the successive point forecasts.
            Otherwise returns a list of historical ``TimeSeries`` forecasts.
        verbose
            Whether to print progress
        Returns
        -------
        TimeSeries or List[TimeSeries]
            By default, a single ``TimeSeries`` instance created from the last point of each individual forecast.
            If `last_points_only` is set to False, a list of the historical forecasts.
        """

        # TODO: do we need a check here? I'd rather leave these checks to the models/datasets.
        # if covariates:
        #     raise_if_not(
        #         series.end_time() <= covariates.end_time() and covariates.start_time() <= series.start_time(),
        #         'The provided covariates must be at least as long as the target series.'
        #     )

        # only GlobalForecastingModels support historical forecastings without retraining the model
        base_class_name = self.__class__.__base__.__name__
        raise_if(
            (isinstance(retrain, Callable) or int(retrain) != 1)
            and (not self._supports_non_retrainable_historical_forecasts()),
            f"{base_class_name} does not support historical forecastings with `retrain` set to `False`. "
            f"For now, this is only supported with GlobalForecastingModels such as TorchForecastingModels. "
            f"For more information, read the documentation for `retrain` in `historical_forecasts()`",
            logger,
        )

        if train_length and not isinstance(train_length, int):
            raise_log(
                TypeError("If not None, train_length needs to be an integer."),
                logger,
            )
        elif (train_length is not None) and train_length < 1:
            raise_log(
                ValueError("If not None, train_length needs to be positive."),
                logger,
            )
        elif (train_length is not None) and train_length < self.min_train_series_length:
            raise_log(
                ValueError(
                    "train_length is too small for the training requirements of this model"
                ),
                logger,
            )

        if isinstance(retrain, bool) or (isinstance(retrain, int) and retrain >= 0):
            retrain_func = _retrain_wrapper(
                lambda counter: counter % int(retrain) == 0 if retrain else False
            )

        elif isinstance(retrain, Callable):
            retrain_func = _retrain_wrapper(retrain)

        else:
            raise_log(
                ValueError(
                    "`retrain` argument must be either `bool`, positive `int` or `Callable` (returning `bool`)"
                ),
                logger,
            )
        retrain_func_signature = tuple(
            inspect.signature(retrain_func).parameters.keys()
        )

        # prepare the start parameter -> pd.Timestamp
        start = series.get_timestamp_at_point(start)

        # build the prediction times in advance (to be able to use tqdm)
        last_valid_pred_time = self._get_last_prediction_time(
            series, forecast_horizon, overlap_end
        )

        pred_times = [start]
        while pred_times[-1] < last_valid_pred_time:
            # compute the next prediction time and add it to pred times
            pred_times.append(pred_times[-1] + series.freq * stride)

        # the last prediction time computed might have overshot last_valid_pred_time
        if pred_times[-1] > last_valid_pred_time:
            pred_times.pop(-1)

        iterator = _build_tqdm_iterator(pred_times, verbose)

        # Either store the whole forecasts or only the last points of each forecast, depending on last_points_only
        forecasts = []

        last_points_times = []
        last_points_values = []

        # iterate and forecast
        for _counter, pred_time in enumerate(iterator):
            # build the training series
            train = series.drop_after(pred_time)
            if train_length and len(train) > train_length:
                train = train[-train_length:]

            # train_cov = covariates.drop_after(pred_time) if covariates else None

            if (not self._fit_called) or retrain_func(
                counter=_counter,
                pred_time=pred_time,
                train_series=train,
                past_covariates=past_covariates.drop_after(pred_time)
                if past_covariates and ("past_covariates" in retrain_func_signature)
                else None,
                future_covariates=future_covariates.drop_after(
                    min(pred_time + series.freq * forecast_horizon, series.end_time())
                )
                if future_covariates and ("future_covariates" in retrain_func_signature)
                else None,
            ):
                self._fit_wrapper(
                    series=train,
                    past_covariates=past_covariates,
                    future_covariates=future_covariates,
                )

            forecast = self._predict_wrapper(
                n=forecast_horizon,
                series=train,
                past_covariates=past_covariates,
                future_covariates=future_covariates,
                num_samples=num_samples,
            )

            if last_points_only:
                last_points_values.append(forecast.all_values(copy=False)[-1])
                last_points_times.append(forecast.end_time())
            else:
                forecasts.append(forecast)

        if last_points_only:
            if series.has_datetime_index:
                return TimeSeries.from_times_and_values(
                    pd.DatetimeIndex(last_points_times, freq=series.freq * stride),
                    np.array(last_points_values),
                    columns=series.columns,
                    static_covariates=series.static_covariates,
                    hierarchy=series.hierarchy,
                )
            else:
                return TimeSeries.from_times_and_values(
                    pd.RangeIndex(
                        start=last_points_times[0],
                        stop=last_points_times[-1] + 1,
                        step=series.freq * stride,
                    ),
                    np.array(last_points_values),
                    columns=series.columns,
                    static_covariates=series.static_covariates,
                    hierarchy=series.hierarchy,
                )

        return forecasts

    def backtest(
        self,
        series: TimeSeries,
        past_covariates: Optional[TimeSeries] = None,
        future_covariates: Optional[TimeSeries] = None,
        num_samples: int = 1,
        train_length: Optional[int] = None,
        start: Union[pd.Timestamp, float, int] = 0.5,
        forecast_horizon: int = 1,
        stride: int = 1,
        retrain: Union[bool, int, Callable[..., bool]] = True,
        overlap_end: bool = False,
        last_points_only: bool = False,
        metric: Union[
            Callable[[TimeSeries, TimeSeries], float],
            List[Callable[[TimeSeries, TimeSeries], float]],
        ] = metrics.mape,
        reduction: Union[Callable[[np.ndarray], float], None] = np.mean,
        verbose: bool = False,
    ) -> Union[float, List[float]]:

        """Compute error values that the model would have produced when
        used on `series`.

        It repeatedly builds a training set from the beginning of `series`. It trains the current model on
        the training set, emits a forecast of length equal to forecast_horizon, and then moves the end of the
        training set forward by `stride` time steps. A metric (given by the `metric` function) is then evaluated
        on the forecast and the actual values. Finally, the method returns a `reduction` (the mean by default)
        of all these metric scores.

        By default, this method uses each historical forecast (whole) to compute error scores.
        If `last_points_only` is set to True, it will use only the last point of each historical
        forecast. In this case, no reduction is used.

        By default, this method always re-trains the models on the entire available history,
        corresponding to an expanding window strategy.
        If `retrain` is set to False (useful for models for which training might be time-consuming, such as
        deep learning models), the model will only be trained on the initial training window
        (up to `start` time stamp), and only if it has not been trained before. Then, at every iteration, the
        newly expanded input sequence will be fed to the model to produce the new output.

        Parameters
        ----------
        series
            The target time series to use to successively train and evaluate the historical forecasts
        past_covariates
            An optional past-observed covariate series. This applies only if the model supports past covariates.
        future_covariates
            An optional future-known covariate series. This applies only if the model supports future covariates.
        num_samples
            Number of times a prediction is sampled from a probabilistic model. Should be left set to 1
            for deterministic models.
        train_length
            Number of time steps in our training set (size of backtesting window to train on).
            Default is set to train_length=None where it takes all available time steps up until prediction time,
            otherwise the moving window strategy is used. If larger than the number of time steps available, all steps
            up until prediction time are used, as in default case. Needs to be at least min_train_series_length.
        start
            The first prediction time, at which a prediction is computed for a future time.
            This parameter supports 3 different types: ``float``, ``int`` and ``pandas.Timestamp``.
            In the case of ``float``, the parameter will be treated as the proportion of the time series
            that should lie before the first prediction point.
            In the case of ``int``, the parameter will be treated as an integer index to the time index of
            `series` that will be used as first prediction time.
            In case of ``pandas.Timestamp``, this time stamp will be used to determine the first prediction time
            directly.
        forecast_horizon
            The forecast horizon for the point prediction.
        stride
            The number of time steps between two consecutive training sets.
        retrain
            Whether and/or on which condition to retrain the model before predicting.
            This parameter supports 3 different datatypes: ``bool``, (positive) ``int``, and
            ``Callable`` (returning a ``bool``).
            In the case of ``bool``: retrain the model at each step (`True`), or never retrains the model (`False`).
            In the case of ``int``: the model is retrained every `retrain` iterations.
            In the case of ``Callable``: the model is retrained whenever callable returns `True`.
            Arguments passed to the callable are as follows:

                - `pred_time (pd.Timestamp or int)`: timestamp of forecast time (end of the training series)
                - `train_series (TimeSeries)`: train series up to `pred_time`
                - `past_covariates (TimeSeries)`: past_covariates series up to `pred_time`
                - `future_covariates (TimeSeries)`: future_covariates series up
                  to `min(pred_time + series.freq * forecast_horizon, series.end_time())`

            Note: some models do require being retrained every time
            and do not support anything else than `retrain=True`.
        overlap_end
            Whether the returned forecasts can go beyond the series' end or not
        last_points_only
            Whether to use the whole historical forecasts or only the last point of each forecast to compute the error
        metric
            A function or a list of function that takes two ``TimeSeries`` instances as inputs and returns an
            error value.
        reduction
            A function used to combine the individual error scores obtained when `last_points_only` is set to False.
            When providing several time-series, the function will receive the argument `axis = 1` to obtain single
            value for each metric function.
            If explicitly set to `None`, the method will return a list of the individual error scores instead.
            Set to ``np.mean`` by default.
        verbose
            Whether to print progress
        Returns
        -------
        float or List[float]
            The error score, or the list of individual error scores if `reduction` is `None`
        """
        forecasts = self.historical_forecasts(
            series=series,
            past_covariates=past_covariates,
            future_covariates=future_covariates,
            num_samples=num_samples,
            train_length=train_length,
            start=start,
            forecast_horizon=forecast_horizon,
            stride=stride,
            retrain=retrain,
            overlap_end=overlap_end,
            last_points_only=last_points_only,
            verbose=verbose,
        )

        if not isinstance(metric, list):
            metric = [metric]

        if last_points_only:
            errors = [metric_f(series, forecasts) for metric_f in metric]

        else:
            # metric in columns, forecast in rows
            errors = [
                [metric_f(series, forecast) for metric_f in metric]
                for forecast in forecasts
            ]
            if reduction is not None:
                # one value per metric
                errors = reduction(np.array(errors), axis=0)

        if len(metric) > 1:
            return errors
        else:
            return errors[0]

    @classmethod
    def gridsearch(
        model_class,
        parameters: dict,
        series: TimeSeries,
        past_covariates: Optional[TimeSeries] = None,
        future_covariates: Optional[TimeSeries] = None,
        forecast_horizon: Optional[int] = None,
        stride: int = 1,
        start: Union[pd.Timestamp, float, int] = 0.5,
        last_points_only: bool = False,
        val_series: Optional[TimeSeries] = None,
        use_fitted_values: bool = False,
        metric: Callable[[TimeSeries, TimeSeries], float] = metrics.mape,
        reduction: Callable[[np.ndarray], float] = np.mean,
        verbose=False,
        n_jobs: int = 1,
        n_random_samples: Optional[Union[int, float]] = None,
    ) -> Tuple["ForecastingModel", Dict[str, Any], float]:
        """
        Find the best hyper-parameters among a given set using a grid search.

        This function has 3 modes of operation: Expanding window mode, split mode and fitted value mode.
        The three modes of operation evaluate every possible combination of hyper-parameter values
        provided in the `parameters` dictionary by instantiating the `model_class` subclass
        of ForecastingModel with each combination, and returning the best-performing model with regard
        to the `metric` function. The `metric` function is expected to return an error value,
        thus the model resulting in the smallest `metric` output will be chosen.

        The relationship of the training data and test data depends on the mode of operation.

        Expanding window mode (activated when `forecast_horizon` is passed):
        For every hyperparameter combination, the model is repeatedly trained and evaluated on different
        splits of `series`. This process is accomplished by using
        the :func:`backtest` function as a subroutine to produce historic forecasts starting from `start`
        that are compared against the ground truth values of `series`.
        Note that the model is retrained for every single prediction, thus this mode is slower.

        Split window mode (activated when `val_series` is passed):
        This mode will be used when the `val_series` argument is passed.
        For every hyper-parameter combination, the model is trained on `series` and
        evaluated on `val_series`.

        Fitted value mode (activated when `use_fitted_values` is set to `True`):
        For every hyper-parameter combination, the model is trained on `series`
        and evaluated on the resulting fitted values.
        Not all models have fitted values, and this method raises an error if the model doesn't have a `fitted_values`
        member. The fitted values are the result of the fit of the model on `series`. Comparing with the
        fitted values can be a quick way to assess the model, but one cannot see if the model is overfitting the series.

        Derived classes must ensure that a single instance of a model will not share parameters with the other
        instances, e.g., saving models in the same path. Otherwise, an unexpected behavior can arise while running
        several models in parallel (when ``n_jobs != 1``). If this cannot be avoided, then gridsearch
        should be redefined, forcing ``n_jobs = 1``.

        Currently this method only supports deterministic predictions (i.e. when models' predictions
        have only 1 sample).

        Parameters
        ----------
        model_class
            The ForecastingModel subclass to be tuned for 'series'.
        parameters
            A dictionary containing as keys hyperparameter names, and as values lists of values for the
            respective hyperparameter.
        series
            The TimeSeries instance used as input and target for training.
        past_covariates
            An optional past-observed covariate series. This applies only if the model supports past covariates.
        future_covariates
            An optional future-known covariate series. This applies only if the model supports future covariates.
        forecast_horizon
            The integer value of the forecasting horizon. Activates expanding window mode.
        stride
            The number of time steps between two consecutive predictions. Only used in expanding window mode.
        start
            The ``int``, ``float`` or ``pandas.Timestamp`` that represents the starting point in the time index
            of `series` from which predictions will be made to evaluate the model.
            For a detailed description of how the different data types are interpreted, please see the documentation
            for `ForecastingModel.backtest`.
        last_points_only
            Whether to use the whole forecasts or only the last point of each forecast to compute the error
        val_series
            The TimeSeries instance used for validation in split mode. If provided, this series must start right after
            the end of `series`; so that a proper comparison of the forecast can be made.
        use_fitted_values
            If `True`, uses the comparison with the fitted values.
            Raises an error if ``fitted_values`` is not an attribute of `model_class`.
        metric
            A function that takes two TimeSeries instances as inputs (actual and prediction, in this order),
            and returns a float error value.
        reduction
            A reduction function (mapping array to float) describing how to aggregate the errors obtained
            on the different validation series when backtesting. By default it'll compute the mean of errors.
        verbose
            Whether to print progress.
        n_jobs
            The number of jobs to run in parallel. Parallel jobs are created only when there are two or more parameters
            combinations to evaluate. Each job will instantiate, train, and evaluate a different instance of the model.
            Defaults to `1` (sequential). Setting the parameter to `-1` means using all the available cores.
        n_random_samples
            The number/ratio of hyperparameter combinations to select from the full parameter grid. This will perform
            a random search instead of using the full grid.
            If an integer, `n_random_samples` is the number of parameter combinations selected from the full grid and
            must be between `0` and the total number of parameter combinations.
            If a float, `n_random_samples` is the ratio of parameter combinations selected from the full grid and must
            be between `0` and `1`. Defaults to `None`, for which random selection will be ignored.

        Returns
        -------
        ForecastingModel, Dict, float
            A tuple containing an untrained `model_class` instance created from the best-performing hyper-parameters,
            along with a dictionary containing these best hyper-parameters,
            and metric score for the best hyper-parameters.
        """
        raise_if_not(
            (forecast_horizon is not None)
            + (val_series is not None)
            + use_fitted_values
            == 1,
            "Please pass exactly one of the arguments 'forecast_horizon', "
            "'val_target_series' or 'use_fitted_values'.",
            logger,
        )

        if use_fitted_values:
            raise_if_not(
                hasattr(model_class(), "fitted_values"),
                "The model must have a fitted_values attribute to compare with the train TimeSeries",
                logger,
            )

        elif val_series is not None:
            raise_if_not(
                series.width == val_series.width,
                "Training and validation series require the same number of components.",
                logger,
            )

        # TODO: here too I'd say we can leave these checks to the models
        # if covariates is not None:
        #     raise_if_not(series.has_same_time_as(covariates), 'The provided series and covariates must have the '
        #                                                       'same time axes.')

        # compute all hyperparameter combinations from selection
        params_cross_product = list(product(*parameters.values()))

        # If n_random_samples has been set, randomly select a subset of the full parameter cross product to search with
        if n_random_samples is not None:
            params_cross_product = model_class._sample_params(
                params_cross_product, n_random_samples
            )

        # iterate through all combinations of the provided parameters and choose the best one
        iterator = _build_tqdm_iterator(
            zip(params_cross_product), verbose, total=len(params_cross_product)
        )

        def _evaluate_combination(param_combination) -> float:
            param_combination_dict = dict(
                list(zip(parameters.keys(), param_combination))
            )
            if param_combination_dict.get("model_name", None):
                current_time = time.strftime("%Y-%m-%d_%H.%M.%S.%f", time.localtime())
                param_combination_dict[
                    "model_name"
                ] = f"{current_time}_{param_combination_dict['model_name']}"

            model = model_class(**param_combination_dict)
            if use_fitted_values:  # fitted value mode
                model._fit_wrapper(series, past_covariates, future_covariates)
                fitted_values = TimeSeries.from_times_and_values(
                    series.time_index, model.fitted_values
                )
                error = metric(series, fitted_values)
            elif val_series is None:  # expanding window mode
                error = model.backtest(
                    series=series,
                    past_covariates=past_covariates,
                    future_covariates=future_covariates,
                    num_samples=1,
                    start=start,
                    forecast_horizon=forecast_horizon,
                    stride=stride,
                    metric=metric,
                    reduction=reduction,
                    last_points_only=last_points_only,
                )
            else:  # split mode
                model._fit_wrapper(series, past_covariates, future_covariates)
                pred = model._predict_wrapper(
                    len(val_series),
                    series,
                    past_covariates,
                    future_covariates,
                    num_samples=1,
                )
                error = metric(val_series, pred)

            return float(error)

        errors: List[float] = _parallel_apply(
            iterator, _evaluate_combination, n_jobs, {}, {}
        )

        min_error = min(errors)

        best_param_combination = dict(
            list(zip(parameters.keys(), params_cross_product[errors.index(min_error)]))
        )

        logger.info("Chosen parameters: " + str(best_param_combination))

        return model_class(**best_param_combination), best_param_combination, min_error

    def residuals(
        self,
        series: TimeSeries,
        past_covariates: Optional[TimeSeries] = None,
        future_covariates: Optional[TimeSeries] = None,
        forecast_horizon: int = 1,
        retrain: bool = True,
        verbose: bool = False,
    ) -> TimeSeries:
        """Compute the residuals produced by this model on a univariate time series.

        This function computes the difference between the actual observations from `series`
        and the fitted values vector `p` obtained by training the model on `series`.
        For every index `i` in `series`, `p[i]` is computed by training the model on
        ``series[:(i - forecast_horizon)]`` and forecasting `forecast_horizon` into the future.
        (`p[i]` will be set to the last value of the predicted series.)
        The vector of residuals will be shorter than `series` due to the minimum
        training series length required by the model and the gap introduced by `forecast_horizon`.
        Most commonly, the term "residuals" implies a value for `forecast_horizon` of 1; but
        this can be configured.

        This method works only on univariate series. It uses the median
        prediction (when dealing with stochastic forecasts).

        Parameters
        ----------
        series
            The univariate TimeSeries instance which the residuals will be computed for.
        past_covariates
            One or several past-observed covariate time series.
        future_covariates
            One or several future-known covariate time series.
        forecast_horizon
            The forecasting horizon used to predict each fitted value.
        retrain
            Whether to train the model at each iteration, for models that support it.
            If False, the model is not trained at all. Default: True
        verbose
            Whether to print progress.
        Returns
        -------
        TimeSeries
            The vector of residuals.
        """
        try:
            series._assert_univariate()
        except (AttributeError, TypeError):
            raise ValueError(
                "series must be of type TimeSeries. "
                "If Sequence[TimeSeries] is provided, select the series to compute residuals for."
            )

        if past_covariates is not None:
            raise_if_not(
                isinstance(past_covariates, TimeSeries),
                "past_covariates should be of type TimeSeries",
            )

        if future_covariates is not None:
            raise_if_not(
                isinstance(future_covariates, TimeSeries),
                "future_covariates should be of type TimeSeries",
            )

        # get first index not contained in the first training set
        first_index = series.time_index[self.min_train_series_length]

        # compute fitted values
        p = self.historical_forecasts(
            series=series,
            past_covariates=past_covariates,
            future_covariates=future_covariates,
            start=first_index,
            forecast_horizon=forecast_horizon,
            stride=1,
            retrain=retrain,
            last_points_only=True,
            verbose=verbose,
        )

        # compute residuals
        series_trimmed = series.slice_intersect(p)
        residuals = series_trimmed - (
            p.quantile_timeseries(quantile=0.5) if p.is_stochastic else p
        )

        return residuals

    @classmethod
    def _sample_params(model_class, params, n_random_samples):
        """Select the absolute number of samples randomly if an integer has been supplied. If a float has been
        supplied, select a fraction"""

        if isinstance(n_random_samples, int):
            raise_if_not(
                (n_random_samples > 0) and (n_random_samples <= len(params)),
                "If supplied as an integer, n_random_samples must be greater than 0 and less"
                "than or equal to the size of the cartesian product of the hyperparameters.",
            )
            return sample(params, n_random_samples)

        if isinstance(n_random_samples, float):
            raise_if_not(
                (n_random_samples > 0.0) and (n_random_samples <= 1.0),
                "If supplied as a float, n_random_samples must be greater than 0.0 and less than 1.0.",
            )
            return sample(params, int(n_random_samples * len(params)))

    def _extract_model_creation_params(self):
        """extracts immutable model creation parameters from `ModelMeta` and deletes reference."""
        model_params = copy.deepcopy(self._model_call)
        del self.__class__._model_call
        return model_params

    def untrained_model(self):
        return self.__class__(**self.model_params)

    @property
    def model_params(self) -> dict:
        return (
            self._model_params if hasattr(self, "_model_params") else self._model_call
        )

    @classmethod
    def _default_save_path(cls) -> str:
        return f"{cls.__name__}_{datetime.datetime.now().strftime('%Y-%m-%d_%H:%M:%S')}"

    def save(self, path: Optional[Union[str, BinaryIO]] = None, **pkl_kwargs) -> None:
        """
        Saves the model under a given path or file handle.

        Example for saving and loading a :class:`RegressionModel`:

            .. highlight:: python
            .. code-block:: python

                from darts.models import RegressionModel

                model = RegressionModel(lags=4)

                model.save("my_model.pkl")
                model_loaded = RegressionModel.load("my_model.pkl")
            ..

        Parameters
        ----------
        path
            Path or file handle under which to save the model at its current state. If no path is specified, the model
            is automatically saved under ``"{ModelClass}_{YYYY-mm-dd_HH:MM:SS}.pkl"``.
            E.g., ``"RegressionModel_2020-01-01_12:00:00.pkl"``.
        pkl_kwargs
            Keyword arguments passed to `pickle.dump()`
        """

        if path is None:
            # default path
            path = self._default_save_path() + ".pkl"

        if isinstance(path, str):
            # save the whole object using pickle
            with open(path, "wb") as handle:
                pickle.dump(obj=self, file=handle, **pkl_kwargs)
        else:
            # save the whole object using pickle
            pickle.dump(obj=self, file=path, **pkl_kwargs)

    @staticmethod
    def load(path: Union[str, BinaryIO]) -> "ForecastingModel":
        """
        Loads the model from a given path or file handle.

        Parameters
        ----------
        path
            Path or file handle from which to load the model.
        """

        if isinstance(path, str):
            raise_if_not(
                os.path.exists(path),
                f"The file {path} doesn't exist",
                logger,
            )

            with open(path, "rb") as handle:
                model = pickle.load(file=handle)
        else:

            model = pickle.load(file=path)

        return model


class LocalForecastingModel(ForecastingModel, ABC):
    """The base class for "local" forecasting models, handling only single univariate time series.

    Local Forecasting Models (LFM) are models that can be trained on a single univariate target series only. In Darts,
    most models in this category tend to be simpler statistical models (such as ETS or FFT). LFMs usually train on
    the entire target series supplied when calling :func:`fit()` at once. They can also predict in one go with
    :func:`predict()` for any number of predictions `n` after the end of the training series.

    All implementations must implement the `_fit()` and `_predict()` methods.
    """

    pass


class GlobalForecastingModel(ForecastingModel, ABC):
    """The base class for "global" forecasting models, handling several time series and optional covariates.

    Global forecasting models expand upon the functionality of `ForecastingModel` in 4 ways:
    1. Models can be fitted on many series (multivariate or univariate) with different indices.
    2. The input series used by :func:`predict()` can be different from the series used to fit the model.
    3. Covariates can be supported (multivariate or univariate).
    4. They can allow for multivariate target series and covariates.

    The name "global" stems from the fact that a training set of a forecasting model of this class is not constrained
    to a temporally contiguous, "local", time series.

    All implementations must implement the :func:`fit()` and :func:`predict()` methods.
    The :func:`fit()` method is meant to train the model on one or several training time series, along with optional
    covariates.

    If :func:`fit()` has been called with only one training and covariate series as argument, then
    calling :func:`predict()` will forecast the future of this series. Otherwise, the user has to
    provide to :func:`predict()` the series they want to forecast, as well as covariates, if needed.
    """

    _expect_past_covariates, _expect_future_covariates = False, False
    past_covariate_series, future_covariate_series = None, None

    def __init__(self, add_encoders: Optional[dict] = None):
        super().__init__()

        # by default models do not use encoders
        self.add_encoders = add_encoders
        self.encoders: Optional[SequentialEncoder] = None

    @abstractmethod
    def fit(
        self,
        series: Union[TimeSeries, Sequence[TimeSeries]],
        past_covariates: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
        future_covariates: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
    ) -> "GlobalForecastingModel":
        """Fit/train the model on (potentially multiple) series.

        Optionally, one or multiple past and/or future covariates series can be provided as well.
        The number of covariates series must match the number of target series.

        Parameters
        ----------
        series
            One or several target time series. The model will be trained to forecast these time series.
            The series may or may not be multivariate, but if multiple series are provided
            they must have the same number of components.
        past_covariates
            One or several past-observed covariate time series. These time series will not be forecast, but can
            be used by some models as an input. The covariate(s) may or may not be multivariate, but if multiple
            covariates are provided they must have the same number of components. If `past_covariates` is provided,
            it must contain the same number of series as `series`.
        future_covariates
            One or several future-known covariate time series. These time series will not be forecast, but can
            be used by some models as an input. The covariate(s) may or may not be multivariate, but if multiple
            covariates are provided they must have the same number of components. If `future_covariates` is provided,
            it must contain the same number of series as `series`.

        Returns
        -------
        self
            Fitted model.
        """

        if isinstance(series, TimeSeries):
            # if only one series is provided, save it for prediction time (including covariates, if available)
            self.training_series = series
            self.static_covariates = series.static_covariates
            if past_covariates is not None:
                self.past_covariate_series = past_covariates
            if future_covariates is not None:
                self.future_covariate_series = future_covariates
        else:
            self.static_covariates = series[0].static_covariates
            if past_covariates is not None:
                self._expect_past_covariates = True
            if future_covariates is not None:
                self._expect_future_covariates = True
        self._fit_called = True

    @abstractmethod
    def predict(
        self,
        n: int,
        series: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
        past_covariates: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
        future_covariates: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
        num_samples: int = 1,
    ) -> Union[TimeSeries, Sequence[TimeSeries]]:
        """Forecasts values for `n` time steps after the end of the series.

        If :func:`fit()` has been called with only one ``TimeSeries`` as argument, then the `series` argument of
        this function is optional, and it will simply produce the next `horizon` time steps forecast.
        The `past_covariates` and `future_covariates` arguments also don't have to be provided again in this case.

        If :func:`fit()` has been called with `series` specified as a ``Sequence[TimeSeries]`` (i.e., the model
        has been trained on multiple time series), the `series` argument must be specified.

        When the `series` argument is specified, this function will compute the next `n` time steps forecasts
        for the simple series (or for each series in the sequence) given by `series`.

        If multiple past or future covariates were specified during the training, some corresponding covariates must
        also be specified here. For every input in `series` a matching (past and/or future) covariate time series
        has to be provided.

        Parameters
        ----------
        n
            Forecast horizon - the number of time steps after the end of the series for which to produce predictions.
        series
            The series whose future values will be predicted.
        past_covariates
            One past-observed covariate time series for every input time series in `series`. They must match the
            past covariates that have been used with the :func:`fit()` function for training in terms of dimension.
        future_covariates
            One future-known covariate time series for every input time series in `series`. They must match the
            past covariates that have been used with the :func:`fit()` function for training in terms of dimension.
        num_samples
            Number of times a prediction is sampled from a probabilistic model. Should be left set to 1
            for deterministic models.

        Returns
        -------
        Union[TimeSeries, Sequence[TimeSeries]]
            If `series` is not specified, this function returns a single time series containing the `n`
            next points after then end of the training series.
            If `series` is given and is a simple ``TimeSeries``, this function returns the `n` next points
            after the end of `series`.
            If `series` is given and is a sequence of several time series, this function returns
            a sequence where each element contains the corresponding `n` points forecasts.
        """
        super().predict(n, num_samples)
        if self._expect_past_covariates and past_covariates is None:
            raise_log(
                ValueError(
                    "The model has been trained with past covariates. Some matching past_covariates "
                    "have to be provided to `predict()`."
                )
            )
        if self._expect_future_covariates and future_covariates is None:
            raise_log(
                ValueError(
                    "The model has been trained with future covariates. Some matching future_covariates "
                    "have to be provided to `predict()`."
                )
            )

    def _predict_wrapper(
        self,
        n: int,
        series: TimeSeries,
        past_covariates: Optional[TimeSeries],
        future_covariates: Optional[TimeSeries],
        num_samples: int,
    ) -> TimeSeries:
        return self.predict(
            n,
            series,
            past_covariates=past_covariates,
            future_covariates=future_covariates,
            num_samples=num_samples,
        )

    def _fit_wrapper(
        self,
        series: TimeSeries,
        past_covariates: Optional[TimeSeries],
        future_covariates: Optional[TimeSeries],
    ):
        self.fit(
            series=series,
            past_covariates=past_covariates if self.uses_past_covariates else None,
            future_covariates=future_covariates
            if self.uses_future_covariates
            else None,
        )

    def _supports_non_retrainable_historical_forecasts(self) -> bool:
        """GlobalForecastingModel supports historical forecasts without retraining the model"""
        return True

    @property
    @abstractmethod
    def _model_encoder_settings(self) -> Tuple[int, int, bool, bool]:
        """Abstract property that returns model specific encoder settings that are used to initialize the encoders.

        Must return Tuple (input_chunk_length, output_chunk_length, takes_past_covariates, takes_future_covariates)
        """
        pass

    def initialize_encoders(self) -> SequentialEncoder:
        """instantiates the SequentialEncoder object based on self._model_encoder_settings and parameter
        ``add_encoders`` used at model creation"""
        (
            input_chunk_length,
            output_chunk_length,
            takes_past_covariates,
            takes_future_covariates,
        ) = self._model_encoder_settings

        return SequentialEncoder(
            add_encoders=self.add_encoders,
            input_chunk_length=input_chunk_length,
            output_chunk_length=output_chunk_length,
            takes_past_covariates=takes_past_covariates,
            takes_future_covariates=takes_future_covariates,
        )

    def generate_fit_encodings(
        self,
        series: Union[TimeSeries, Sequence[TimeSeries]],
        past_covariates: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
        future_covariates: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
    ) -> Tuple[
        Union[TimeSeries, Sequence[TimeSeries]], Union[TimeSeries, Sequence[TimeSeries]]
    ]:
        """Generates the covariate encodings that were used/generated for fitting the model and returns a tuple of
        past, and future covariates series with the original and encoded covariates stacked together. The encodings are
        generated by the encoders defined at model creation with parameter `add_encoders`. Pass the same `series`,
        `past_covariates`, and  `future_covariates` that you used to train/fit the model.

        Parameters
        ----------
        series
            The series or sequence of series with the target values used when fitting the model.
        past_covariates
            Optionally, the series or sequence of series with the past-observed covariates used when fitting the model.
        future_covariates
            Optionally, the series or sequence of series with the future-known covariates used when fitting the model.

        Returns
        -------
        Tuple[Union[TimeSeries, Sequence[TimeSeries]], Union[TimeSeries, Sequence[TimeSeries]]]
            A tuple of (past covariates, future covariates). Each covariate contains the original as well as the
            encoded covariates.
        """
        raise_if(
            self.encoders is None or not self.encoders.encoding_available,
            "Encodings are not available. Consider adding parameter `add_encoders` at model creation and fitting the "
            "model with `model.fit()` before.",
            logger=logger,
        )
        return self.encoders.encode_train(
            target=series,
            past_covariates=past_covariates,
            future_covariates=future_covariates,
        )

    def generate_predict_encodings(
        self,
        n: int,
        series: Union[TimeSeries, Sequence[TimeSeries]],
        past_covariates: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
        future_covariates: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
    ) -> Tuple[
        Union[TimeSeries, Sequence[TimeSeries]], Union[TimeSeries, Sequence[TimeSeries]]
    ]:
        """Generates covariate encodings for the inference/prediction set and returns a tuple of past, and future
        covariates series with the original and encoded covariates stacked together. The encodings are generated by the
        encoders defined at model creation with parameter `add_encoders`. Pass the same `series`, `past_covariates`,
        and `future_covariates` that you intend to use for prediction.

        Parameters
        ----------
        n
            The number of prediction time steps after the end of `series` intended to be used for prediction.
        series
            The series or sequence of series with target values intended to be used for prediction.
        past_covariates
            Optionally, the past-observed covariates series intended to be used for prediction. The dimensions must
            match those of the covariates used for training.
        future_covariates
            Optionally, the future-known covariates series intended to be used for prediction. The dimensions must
            match those of the covariates used for training.

        Returns
        -------
        Tuple[Union[TimeSeries, Sequence[TimeSeries]], Union[TimeSeries, Sequence[TimeSeries]]]
            A tuple of (past covariates, future covariates). Each covariate contains the original as well as the
            encoded covariates.
        """
        raise_if(
            self.encoders is None or not self.encoders.encoding_available,
            "Encodings are not available. Consider adding parameter `add_encoders` at model creation and fitting the "
            "model with `model.fit()` before.",
            logger=logger,
        )
        return self.encoders.encode_inference(
            n=self._get_encoders_n(n),
            target=series,
            past_covariates=past_covariates,
            future_covariates=future_covariates,
        )

    def _get_encoders_n(self, n) -> int:
        """Returns the number of prediction steps for generating with `model.encoders.generate_predict_encodings()`.
        Subclasses can have different requirements for setting `n`. The most general case simply returns `n` as is.
        """
        return n


class FutureCovariatesLocalForecastingModel(LocalForecastingModel, ABC):
    """The base class for future covariates "local" forecasting models, handling single uni- or multivariate target
    and optional future covariates time series.

    Future Covariates Local Forecasting Models (FC-LFM) are models that can be trained on a single uni- or multivariate
    target and optional future covariates series. In Darts, most models in this category tend to be simpler statistical
    models (such as ARIMA). FC-LFMs usually train on the entire target and future covariates series supplied when
    calling :func:`fit()` at once. They can also predict in one go with :func:`predict()` for any number of predictions
    `n` after the end of the training series. When using future covariates, the values for the future `n` prediction
    steps must be given in the covariate series.

    All implementations must implement the :func:`_fit()` and :func:`_predict()` methods.
    """

    _expect_covariate = False

    def fit(self, series: TimeSeries, future_covariates: Optional[TimeSeries] = None):
        """Fit/train the model on the (single) provided series.

        Optionally, a future covariates series can be provided as well.

        Parameters
        ----------
        series
            The model will be trained to forecast this time series. Can be multivariate if the model supports it.
        future_covariates
            A time series of future-known covariates. This time series will not be forecasted, but can be used by
            some models as an input. It must contain at least the same time steps/indices as the target `series`.
            If it is longer than necessary, it will be automatically trimmed.

        Returns
        -------
        self
            Fitted model.
        """

        if future_covariates is not None:
            if not series.has_same_time_as(future_covariates):
                # fit() expects future_covariates to have same time as the target, so we intersect it here
                future_covariates = future_covariates.slice_intersect(series)

            raise_if_not(
                series.has_same_time_as(future_covariates),
                "The provided `future_covariates` series must contain at least the same time steps/"
                "indices as the target `series`.",
                logger,
            )
            self._expect_covariate = True

        super().fit(series)

        return self._fit(series, future_covariates=future_covariates)

    @abstractmethod
    def _fit(self, series: TimeSeries, future_covariates: Optional[TimeSeries] = None):
        """Fits/trains the model on the provided series.
        DualCovariatesModels must implement the fit logic in this method.
        """
        pass

    def predict(
        self,
        n: int,
        future_covariates: Optional[TimeSeries] = None,
        num_samples: int = 1,
        **kwargs,
    ) -> TimeSeries:
        """Forecasts values for `n` time steps after the end of the training series.

        If some future covariates were specified during the training, they must also be specified here.

        Parameters
        ----------
        n
            Forecast horizon - the number of time steps after the end of the series for which to produce predictions.
        future_covariates
            The time series of future-known covariates which can be fed as input to the model. It must correspond to
            the covariate time series that has been used with the :func:`fit()` method for training, and it must
            contain at least the next `n` time steps/indices after the end of the training target series.
        num_samples
            Number of times a prediction is sampled from a probabilistic model. Should be left set to 1
            for deterministic models.

        Returns
        -------
        TimeSeries, a single time series containing the `n` next points after then end of the training series.
        """

        super().predict(n, num_samples)

        if self._expect_covariate and future_covariates is None:
            raise_log(
                ValueError(
                    "The model has been trained with `future_covariates` variable. Some matching "
                    "`future_covariates` variables have to be provided to `predict()`."
                )
            )

        raise_if(
            not self._expect_covariate and future_covariates is not None,
            "The model has been trained without `future_covariates` variable, but the "
            "`future_covariates` parameter provided to `predict()` is not None.",
        )

        if future_covariates is not None:
            start = self.training_series.end_time() + self.training_series.freq

            invalid_time_span_error = (
                f"For the given forecasting horizon `n={n}`, the provided `future_covariates` "
                f"series must contain at least the next `n={n}` time steps/indices after the "
                f"end of the target `series` that was used to train the model."
            )

            # we raise an error here already to avoid getting error from empty TimeSeries creation
            raise_if_not(
                future_covariates.end_time() >= start, invalid_time_span_error, logger
            )

            offset = (
                n - 1
                if isinstance(future_covariates.time_index, pd.DatetimeIndex)
                else n
            )
            future_covariates = future_covariates[
                start : start + offset * self.training_series.freq
            ]

            raise_if_not(
                len(future_covariates) == n,
                invalid_time_span_error,
                logger,
            )

        return self._predict(
            n, future_covariates=future_covariates, num_samples=num_samples, **kwargs
        )

    @abstractmethod
    def _predict(
        self,
        n: int,
        future_covariates: Optional[TimeSeries] = None,
        num_samples: int = 1,
    ) -> TimeSeries:
        """Forecasts values for a certain number of time steps after the end of the series.
        DualCovariatesModels must implement the predict logic in this method.
        """
        pass

    def _fit_wrapper(
        self,
        series: TimeSeries,
        past_covariates: Optional[TimeSeries],
        future_covariates: Optional[TimeSeries],
    ):
        self.fit(series, future_covariates=future_covariates)

    def _predict_wrapper(
        self,
        n: int,
        series: TimeSeries,
        past_covariates: Optional[TimeSeries],
        future_covariates: Optional[TimeSeries],
        num_samples: int,
    ) -> TimeSeries:
        return self.predict(
            n, future_covariates=future_covariates, num_samples=num_samples
        )


class TransferableFutureCovariatesLocalForecastingModel(
    FutureCovariatesLocalForecastingModel, ABC
):
    """The base class for transferable future covariates "local" forecasting models, handling single uni- or
    multivariate target and optional future covariates time series. Additionally, at prediction time, it can be
    applied to new data unrelated to the original series used for fitting the model.

    Transferable Future Covariates Local Forecasting Models (TFC-LFM) are models that can be trained on a single uni-
    or multivariate target and optional future covariates series. Additionally, at prediction time, it can be applied
    to new data unrelated to the original series used for fitting the model. Currently in Darts, all models in this
    category wrap to statsmodel models such as VARIMA. TFC-LFMs usually train on the entire target and future covariates
    series supplied when calling :func:`fit()` at once. They can also predict in one go with :func:`predict()`
    for any number of predictions `n` after the end of the training series. When using future covariates, the values
    for the future `n` prediction steps must be given in the covariate series.

    All implementations must implement the :func:`_fit()` and :func:`_predict()` methods.
    """

    def predict(
        self,
        n: int,
        series: Optional[TimeSeries] = None,
        future_covariates: Optional[TimeSeries] = None,
        num_samples: int = 1,
        **kwargs,
    ) -> TimeSeries:
        """If the `series` parameter is not set, forecasts values for `n` time steps after the end of the training
        series. If some future covariates were specified during the training, they must also be specified here.

        If the `series` parameter is set, forecasts values for `n` time steps after the end of the new target
        series. If some future covariates were specified during the training, they must also be specified here.

        Parameters
        ----------
        n
            Forecast horizon - the number of time steps after the end of the series for which to produce predictions.
        series
            Optionally, a new target series whose future values will be predicted. Defaults to `None`, meaning that the
            model will forecast the future value of the training series.
        future_covariates
            The time series of future-known covariates which can be fed as input to the model. It must correspond to
            the covariate time series that has been used with the :func:`fit()` method for training.

            If `series` is not set, it must contain at least the next `n` time steps/indices after the end of the
            training target series. If `series` is set, it must contain at least the time steps/indices corresponding
            to the new target series (historic future covariates), plus the next `n` time steps/indices after the end.
        num_samples
            Number of times a prediction is sampled from a probabilistic model. Should be left set to 1
            for deterministic models.

        Returns
        -------
        TimeSeries, a single time series containing the `n` next points after then end of the training series.
        """

        if self._expect_covariate and future_covariates is None:
            raise_log(
                ValueError(
                    "The model has been trained with `future_covariates` variable. Some matching "
                    "`future_covariates` variables have to be provided to `predict()`."
                )
            )

        historic_future_covariates = None

        if series is not None and future_covariates:
            raise_if_not(
                future_covariates.start_time() <= series.start_time()
                and future_covariates.end_time() >= series.end_time() + n * series.freq,
                "The provided `future_covariates` related to the new target series must contain at least the same time"
                "steps/indices as the target `series` + `n`.",
                logger,
            )
            # splitting the future covariates
            (
                historic_future_covariates,
                future_covariates,
            ) = future_covariates.split_after(series.end_time())

            # in case future covariates have more values on the left end side that we don't need
            if not series.has_same_time_as(historic_future_covariates):
                historic_future_covariates = historic_future_covariates.slice_intersect(
                    series
                )

        # FutureCovariatesLocalForecastingModel performs some checks on self.training_series. We temporary replace
        # that with the new ts
        if series is not None:
            self._orig_training_series = self.training_series
            self.training_series = series

        result = super().predict(
            n=n,
            series=series,
            historic_future_covariates=historic_future_covariates,
            future_covariates=future_covariates,
            num_samples=num_samples,
            **kwargs,
        )

        # restoring the original training ts
        if series is not None:
            self.training_series = self._orig_training_series

        return result

    @abstractmethod
    def _predict(
        self,
        n: int,
        series: Optional[TimeSeries] = None,
        historic_future_covariates: Optional[TimeSeries] = None,
        future_covariates: Optional[TimeSeries] = None,
        num_samples: int = 1,
    ) -> TimeSeries:
        """Forecasts values for a certain number of time steps after the end of the series.
        TransferableFutureCovariatesLocalForecastingModel must implement the predict logic in this method.
        """
        pass

    def _predict_wrapper(
        self,
        n: int,
        series: TimeSeries,
        past_covariates: Optional[TimeSeries],
        future_covariates: Optional[TimeSeries],
        num_samples: int,
    ) -> TimeSeries:
        return self.predict(
            n=n,
            series=series,
            future_covariates=future_covariates,
            num_samples=num_samples,
        )

    def _supports_non_retrainable_historical_forecasts(self) -> bool:
        return True
