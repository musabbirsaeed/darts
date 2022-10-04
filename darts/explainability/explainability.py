"""
Forecasting Model Explainer Base Class

A forecasting model explainer takes a fitted forecasting model as input and applies an Explainability model
to it. Its purpose is to explain each past input contribution to a given model forecast. This 'explanation'
depends on the characteristics of the XAI model chosen (shap, lime etc...).

"""
from abc import ABC, abstractmethod
from typing import Collection, Dict, Optional, Sequence, Union

from numpy import integer

from darts import TimeSeries
from darts.logging import get_logger, raise_if, raise_if_not, raise_log
from darts.models.forecasting.forecasting_model import ForecastingModel
from darts.utils.statistics import stationarity_tests
from darts.utils.utils import series2seq

logger = get_logger(__name__)

MIN_BACKGROUND_SAMPLE = 10


class ExplainabilityResult(ABC):
    """
    Class to store the explainability results of a `ForecastingModelExplainer`, and to
    easily access the results.
    """

    def __init__(
        self,
        explained_forecasts: Union[
            Dict[integer, Dict[str, TimeSeries]],
            Sequence[Dict[integer, Dict[str, TimeSeries]]],
        ],
    ):

        self.explained_forecasts = explained_forecasts
        if isinstance(self.explained_forecasts, list):
            self.available_horizons = list(self.explained_forecasts[0].keys())
            h_0 = self.available_horizons[0]
            self.available_components = list(self.explained_forecasts[0][h_0].keys())
        else:
            self.available_horizons = list(self.explained_forecasts.keys())
            h_0 = self.available_horizons[0]
            self.available_components = list(self.explained_forecasts[h_0].keys())

    def get_explanation(
        self, horizon: int, component: Optional[str] = None
    ) -> Union[TimeSeries, Sequence[TimeSeries]]:

        raise_if(
            component is None and len(self.available_components) > 1,
            ValueError(
                "The component parameter is required when the model has more than one component."
            ),
            logger,
        )

        if component is None:
            component = self.available_components[0]

        raise_if_not(
            horizon in self.available_horizons,
            "Horizon {} is not available. Available horizons are: {}".format(
                horizon, self.available_horizons
            ),
        )

        raise_if_not(
            component in self.available_components,
            "Component {} is not available. Available components are: {}".format(
                component, self.available_components
            ),
        )

        if isinstance(self.explained_forecasts, list):
            return [
                self.explained_forecasts[i][horizon][component]
                for i in range(len(self.explained_forecasts))
            ]
        else:
            return self.explained_forecasts[horizon][component]


class ForecastingModelExplainer(ABC):
    @abstractmethod
    def __init__(
        self,
        model: ForecastingModel,
        background_series: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
        background_past_covariates: Optional[
            Union[TimeSeries, Sequence[TimeSeries]]
        ] = None,
        background_future_covariates: Optional[
            Union[TimeSeries, Sequence[TimeSeries]]
        ] = None,
    ):
        """The base class for forecasting model explainers. It defines the *minimal* behavior that all
        forecasting model explainers support.

        Naming:
        - A background series is a `TimeSeries` with which we 'train' the `Explainer` model.

        - A foreground series is the `TimeSeries` we will explain according to the fitted `Explainer` model.

        Parameters
        ----------
        model
            A `ForecastingModel` we want to explain. It must be fitted first.
        background_series
            A series or list of series to *train* the `ForecastingModelExplainer` along with any foreground series.
            Consider using a reduced well-chosen background to reduce computation time.
                - optional if `model` was fit on a single target series. By default, it is the `series` used
                at fitting time.

                - mandatory if `model` was fit on multiple (sequence of) target series.
        background_past_covariates
            A past covariates series or list of series that the model needs once fitted.
        background_future_covariates
            A future covariates series or list of series that the model needs once fitted.
        """
        if not model._fit_called:
            raise_log(
                ValueError(
                    "The model must be fitted before instantiating a ForecastingModelExplainer."
                ),
                logger,
            )

        if model._is_probabilistic():
            logger.warning(
                "The model is probabilistic, but num_samples=1 will be used for explainability."
            )

        self.model = model

        # if `background_series` was not passed, use `training_series` saved in fitted forecasting model.
        if background_series is None:

            raise_if(
                (background_past_covariates is not None)
                or (background_future_covariates is not None),
                "Supplied background past or future covariates but no background series. Please provide "
                "`background_series`.",
            )

            raise_if(
                self.model.training_series is None,
                "`background_series` must be provided if `model` was fit on multiple time series.",
            )

            background_series = self.model.training_series
            background_past_covariates = self.model.past_covariate_series
            background_future_covariates = self.model.future_covariate_series

        else:
            if self.model.encoders.encoding_available:
                (
                    background_past_covariates,
                    background_future_covariates,
                ) = self.model.generate_predict_encodings(
                    n=len(background_series) - self.model.min_train_series_length,
                    series=background_series,
                    past_covariates=background_past_covariates,
                    future_covariates=background_future_covariates,
                )

        self.background_series = series2seq(background_series)
        self.background_past_covariates = series2seq(background_past_covariates)
        self.background_future_covariates = series2seq(background_future_covariates)

        if self.model.uses_past_covariates:
            raise_if(
                self.model._expect_past_covariates
                and self.background_past_covariates is None,
                "A background past covariates is not provided, but the model needs past covariates.",
            )

        if self.model.uses_future_covariates:
            raise_if(
                self.model._expect_future_covariates
                and self.background_future_covariates is None,
                "A background future covariates is not provided, but the model needs future covariates.",
            )

        self.target_components = self.background_series[0].columns.to_list()
        self.past_covariates_components = None
        if self.background_past_covariates is not None:
            self.past_covariates_components = self.background_past_covariates[
                0
            ].columns.to_list()
        self.future_covariates_components = None
        if self.background_future_covariates is not None:
            self.future_covariates_components = self.background_future_covariates[
                0
            ].columns.to_list()

        self._check_background_covariates(
            self.background_series,
            self.background_past_covariates,
            self.background_future_covariates,
            self.target_components,
            self.past_covariates_components,
            self.future_covariates_components,
        )

        if not self._test_stationarity():
            logger.warning(
                "At least one time series component of the background time series is not stationary."
                " Beware of wrong interpretation with chosen explainability."
            )

    @staticmethod
    def _check_background_covariates(
        background_series,
        background_past_covariates,
        background_future_covariates,
        target_components,
        past_covariates_components,
        future_covariates_components,
    ) -> None:

        if background_past_covariates is not None:
            raise_if_not(
                len(background_series) == len(background_past_covariates),
                "The number of background series and past covariates must be the same.",
            )

        if background_future_covariates is not None:
            raise_if_not(
                len(background_series) == len(background_future_covariates),
                "The number of background series and future covariates must be the same.",
            )

        # ensure we have the same names between TimeSeries (if list of). Important to ensure homogeneity
        # for explained features.
        for idx in range(len(background_series)):
            raise_if_not(
                all(
                    [
                        background_series[idx].columns.to_list() == target_components,
                        background_past_covariates[idx].columns.to_list()
                        == past_covariates_components
                        if background_past_covariates is not None
                        else True,
                        background_future_covariates[idx].columns.to_list()
                        == future_covariates_components
                        if background_future_covariates is not None
                        else True,
                    ]
                ),
                "Columns names must be identical between TimeSeries list components (multi-TimeSeries).",
            )

    @abstractmethod
    def explain(
        self,
        foreground_series: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
        foreground_past_covariates: Optional[
            Union[TimeSeries, Sequence[TimeSeries]]
        ] = None,
        foreground_future_covariates: Optional[
            Union[TimeSeries, Sequence[TimeSeries]]
        ] = None,
        horizons: Optional[Collection[int]] = None,
        target_components: Optional[Collection[str]] = None,
    ) -> ExplainabilityResult:
        """
        Main method of the ForecastingExplainer class.
        Return a ExplainabilityResult instance.

        Results can be retrieved via the ExplainabilityResult.get_explanation(horizon, target_component)
        The result is a multivariate `TimeSeries` instance containing the 'explanation'
        for the (horizon, target_component) forecast at any timestamp forecastable corresponding to
        the foreground `TimeSeries` input.

        The component name convention of this multivariate `TimeSeries` is:
        ``f'{name}_{type_of_cov}_{lag}_{int}'``, where:

        - `name` is the component name from the original foreground series (target, past, or future).
        - `type_of_cov` is the covariates type. It can take 3 different values: ``{"target", "past", "future"}``.
        - `int` is the lag index.

        Example:
        Let's say we have a model with 2 target components we named ``"T_0"`` and ``"T_1"``,
        three past covariates with default component names
        (which will be 0, 1 and 2 by default at initialization),
        and one future covariate with default component name (0).
        Also, ``horizons = [1, 2]``.
        The model is a regression model, with ``lags = 3``, ``lags_past_covariates=[-1, -3]``,
        ``lags_future_covariates = [0]``.

        We provide `foreground_series`, `foreground_past_covariates`, `foreground_future_covariates` each of length 5.


        >>> explain_results = explainer.explain(
        foreground_series=foreground_series,
        foreground_past_covariates=foreground_past_covariates,
        foreground_future_covariates=foreground_future_covariates,
        horizons=[1, 2],
        target_names=["T_0", "T_1"])
        >>> output = explain_results.get_explanation(horizon=1, target="T_1")

        Then ``output`` is a multivariate TimeSeries containing the **explanations** of the corresponding
        `ForecastingModelExplainer`,
         with component names:
             - T_0_target_lag-1
             - T_0_target_lag-2
             - T_0_target_lag-3
             - T_1_target_lag-1
             - T_1_target_lag-2
             - T_1_target_lag-3
             - 0_past_cov_lag-1
             - 0_past_cov_lag-3
             - 1_past_cov_lag-1
             - 1_past_cov_lag-3
             - 2_past_cov_lag-1
             - 2_past_cov_lag-3
             - 0_fut_cov_lag_0

         of length 3, as we can explain 5-3+1 forecasts (basically timestamp indexes 4, 5, and 6)

         Parameters
         ----------
        foreground_series
            Optionally, the target `TimeSeries` to be explained. Can be multivariate.
            If none is provided, the background `TimeSeries` will be explained instead.
        foreground_past_covariates
            Optionally, past covariate timeseries if needed by the ForecastingModel.
        foreground_future_covariates
            Optionally, future covariate timeseries if needed by the ForecastingModel.
        horizons
            Optionally, a collection of integers representing the future lags to be explained.
            Horizon 1 corresponds to the first timestamp being forecasted.
            All values must be no larger than `output_chunk_length` of the explained model.
        target_components
            Optionally, A list of string naming the target components we want to explain.

         Returns
         -------
         ExplainabilityResult
             The forecast explanations.

        """
        pass

    def _test_stationarity(self):
        return all(
            [
                (
                    stationarity_tests(background_serie[c])
                    for c in background_serie.components
                )
                for background_serie in self.background_series
            ]
        )
