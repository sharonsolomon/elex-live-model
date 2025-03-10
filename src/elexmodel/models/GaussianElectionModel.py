import numpy as np
import pandas as pd
from scipy import stats

from elexmodel.distributions.GaussianModel import GaussianModel
from elexmodel.models.BaseElectionModel import BaseElectionModel, PredictionIntervals


class GaussianElectionModel(BaseElectionModel):
    def __init__(self, model_settings={}):
        super().__init__(model_settings)
        self.model_settings = model_settings
        self.beta = model_settings.get("beta", 1)

    def _compute_conf_frac(self):
        """
        Compute conformalization fraction for Gaussian model
        """
        return 0.7

    def get_minimum_reporting_units(self, alpha):
        return 10 * self._compute_conf_frac()

    def get_unit_prediction_intervals(self, reporting_units, nonreporting_units, alpha, estimand):
        """
        Get unit prediction intervals in Gaussian case. Adjust unit prediction intervals based on Gaussian model
        that is fit to conformalization data.
        """
        conf_frac = self._compute_conf_frac()
        # compute unadjusted upper/lower unit bounds and get conformalization data
        prediction_intervals = self.get_unit_prediction_interval_bounds(
            reporting_units, nonreporting_units, conf_frac, alpha, estimand
        )

        # fit gaussian model to nonconformity scores
        gaussian_model = GaussianModel(self.model_settings).fit(
            prediction_intervals.conformalization,
            reporting_units,
            nonreporting_units,
            estimand,
            aggregate=[],
            alpha=alpha,
            beta=self.beta,
        )

        # gaussian model for single unit prediction intervals
        quantile = (3 + alpha) / 4
        lower_correction = stats.norm.ppf(
            q=quantile,
            loc=gaussian_model.mu_lower_bound,
            scale=np.sqrt(gaussian_model.var_inflate + 1) * gaussian_model.sigma_lower_bound,
        )
        upper_correction = stats.norm.ppf(
            q=quantile,
            loc=gaussian_model.mu_upper_bound,
            scale=np.sqrt(gaussian_model.var_inflate + 1) * gaussian_model.sigma_upper_bound,
        )

        # save for later, but need to copy to avoid changing the original
        self.nonreporting_lower_bounds = prediction_intervals.lower.copy()
        self.nonreporting_upper_bounds = prediction_intervals.upper.copy()

        # apply correction
        lower = prediction_intervals.lower - lower_correction
        upper = prediction_intervals.upper + upper_correction

        # un-normalize residuals
        lower *= nonreporting_units[f"total_voters_{estimand}"]
        upper *= nonreporting_units[f"total_voters_{estimand}"]

        # move from residual to vote space
        # max with nonreporting results so that bounds are at least as large as the # of votes seen
        lower = np.maximum(
            lower + nonreporting_units[f"last_election_results_{estimand}"], nonreporting_units[f"results_{estimand}"]
        )
        upper = np.maximum(
            upper + nonreporting_units[f"last_election_results_{estimand}"], nonreporting_units[f"results_{estimand}"]
        )

        return PredictionIntervals(
            lower.round(decimals=0), upper.round(decimals=0), prediction_intervals.conformalization
        )

    def get_aggregate_prediction_intervals(
        self,
        reporting_units,
        nonreporting_units,
        unexpected_units,
        aggregate,
        alpha,
        conformalization_data,
        estimand,
        model_settings,
    ):
        """
        Get aggregate prediction intervals. Adjust aggregate prediction intervals based on Gaussian models
        that are fit to conformalization data per group.
        """

        # get reporting votes by aggregate
        aggregate_votes = self._get_reporting_aggregate_votes(reporting_units, unexpected_units, aggregate, estimand)

        # get non reporting votes by aggregate (votes cast in units that haven't met reporting threshold
        # yet, but still have returns)
        aggregate_nonreporting_votes = self._get_nonreporting_aggregate_votes(nonreporting_units, aggregate)

        # get last election results by aggregate (for un-residualizing later)
        last_election = (
            nonreporting_units.groupby(aggregate)
            .agg(**{f"last_election_results_{estimand}": (f"last_election_results_{estimand}", "sum")})
            .reset_index(drop=False)
        )

        # fit gaussian model in aggregate case
        gaussian_model = GaussianModel(model_settings).fit(
            conformalization_data,
            reporting_units,
            nonreporting_units,
            estimand,
            aggregate=aggregate,
            alpha=alpha,
            reweight=False,
            beta=self.beta,
            top_level=True,
        )

        # if there are no nonreporting units, we can return just the aggregated votes
        if nonreporting_units.shape[0] == 0:
            return aggregate_votes[f"results_{estimand}"], aggregate_votes[f"results_{estimand}"]

        # assign nonreporting unadjusted lower/upper bounds to unobsered data
        bounds = nonreporting_units.assign(
            nonreporting_lower_bounds=self.nonreporting_lower_bounds,
            nonreporting_upper_bounds=self.nonreporting_upper_bounds,
        )

        # un-normalize unadjusted lower/upper bounds and sum per group to get unadjusted group bounds
        # also aggregate sum w_i^2 and sum w_i per group for future use
        bounds = (
            bounds.groupby(aggregate)
            .apply(
                lambda x: pd.Series(
                    {
                        "nonreporting_aggregate_lower_bound": np.sum(
                            x[f"total_voters_{estimand}"] * x.nonreporting_lower_bounds
                        ),
                        "nonreporting_aggregate_upper_bound": np.sum(
                            x[f"total_voters_{estimand}"] * x.nonreporting_upper_bounds
                        ),
                        "nonreporting_weight_sum": np.sum(x[f"total_voters_{estimand}"]),
                        "nonreporting_weight_ssum": np.sum(np.power(x[f"total_voters_{estimand}"], 2)),
                    }
                )
            )
            .reset_index(drop=False)
        )

        # Combine each group in aggregate with appriopriate Gaussian model. This is complicated :(

        # example with three levels [postal_code, county_classification, county_fips]
        # step 1: find all rows in g_model with NA in the most granular level (e.g. county_fips)
        # step 2: find all rows in bounds that do not have a match with the most granular level model (e.g. county_fips)
        # step 3: match unmatched bounds (from step 2) to available models one agg level above (identified in step 1)
        # step 4: go back to step one, but now for [postal_code, county_classification]
        # IMPORTANT NOTE: the model is not currently using more than two levels in this function.
        # While the model AS A WHOLE can have any number of aggregation levels, this function at maximum
        # takes in a list of two (the current agg level + 'postal_code') at a time.

        # first join gaussian model based on *all* aggregates (that is groups with enough units)
        modeled_bounds = bounds.merge(gaussian_model, how="inner", on=aggregate)

        # for groups that did not have enough examples, we want to join the models trained on larger aggregations
        # (ie. postal_code instead of postal_code, county_classification)
        # In loop below, i: index of level of aggregation we are now trying to match models to
        # Note that i is not attached to the aggregation level in the whole MODEL we are currently
        # working on. For a given aggregation level, i is looping over the list that includes that agg level
        # AND higher agg levels so gaussians can be fit.
        for i in range(1, len(aggregate) + 1):
            # In each loop iteration we get the remaining bounds (those that need to be matched),
            # the remaining models (those that are available to be matched) and merge the two.
            # modeled_bounds is then appended with any bounds that have been newly matched.

            # last_i_aggregate is level of aggregation we are now trying to match (ie. county_fips)
            last_i_aggregate = aggregate[len(aggregate) - i :]  # noqa: E203

            # GET REMAINING MODELS
            # We are interested in models at the next highest aggregation level
            # after the level in this loop interation. Therefore we don't care about
            # models in the gaussian df that are the current agg level.
            # (ie. we want postal_code models if last_i_aggregate is county_fips)
            remaining_models_idx = pd.isnull(gaussian_model[last_i_aggregate]).all(axis=1)
            remaining_models = gaussian_model[remaining_models_idx].reset_index(drop=True)

            # since remaining_models[last_i_aggregate] is null, we drop it
            remaining_models.drop(columns=last_i_aggregate, inplace=True)

            # the aggregation level below (ie. full aggregation)
            previous_aggregate = aggregate[: len(aggregate) - i + 1]
            # the aggregation level above (ie. postal_code)
            next_aggregate = previous_aggregate[:-1]

            # GET REMAINING BOUNDS
            # Get bounds of groups that don't have a model yet
            # that is get indices (and then elements) of groups that DON'T
            # appear in both bounds (all groups) and modeled bounds (groups that already have a model)
            # These are therefore the remaining bounds we need to match.
            remaining_bounds_idx = (
                bounds.merge(modeled_bounds, how="left", on=aggregate, indicator=True).query("_merge != 'both'").index
            )

            remaining_bounds = bounds.iloc[remaining_bounds_idx].reset_index(drop=True)
            # First step is to assign to merge the remaining models onto the remaining bounds
            # In the case where aggregate == 1, next_aggregate is only an empty list and we

            # MERGE REMAINING BOUNDS AND REMAINING MODELS
            # Merge the remaining bounds onto the available models. Remaining bounds are the bounds without
            # a model and the available models are the models for next highest agg level.
            # In the case where there is no higher level of aggregation (i.e. we reach the end
            # of 'aggregate'), we want to use the model constructed from ALL combined reporting
            # units. In this case, next_aggregate is an empty list, but remaining_models only has
            # one element, so we can cross merge that element to all rows in remaining_bounds.
            # Note that the same model can be matched to multiple bounds at a more granular agg level!
            if len(next_aggregate) == 0:
                assert remaining_models.shape[0] <= 1
                remaining_bounds_w_models = remaining_bounds.merge(remaining_models, how="cross")
            else:

                remaining_bounds_w_models = remaining_bounds.merge(remaining_models, how="inner", on=next_aggregate)
            # APPEND NEWLY MODELED BOUNDS TO modeled_bounds
            modeled_bounds = pd.concat([modeled_bounds, remaining_bounds_w_models])

        # construction conformal corrections using Gaussian models
        # get means and standard deviations for for aggregates
        # and add correction (which is percentile of the Gaussian at the quantile we care about)
        quantile = (3 + alpha) / 4
        modeled_bounds = modeled_bounds.assign(
            lb_mean=lambda x: x.nonreporting_weight_sum * x.mu_lower_bound,
            lb_sd=lambda x: x.sigma_lower_bound
            * np.sqrt(x.nonreporting_weight_ssum + x.var_inflate * np.power(x.nonreporting_weight_sum, 2)),
            ub_mean=lambda x: x.nonreporting_weight_sum * x.mu_upper_bound,
            ub_sd=lambda x: x.sigma_upper_bound
            * np.sqrt(x.nonreporting_weight_ssum + x.var_inflate * np.power(x.nonreporting_weight_sum, 2)),
        ).assign(
            lb=lambda x: x.nonreporting_aggregate_lower_bound
            - stats.norm.ppf(q=quantile, loc=x.lb_mean, scale=x.lb_sd),
            ub=lambda x: x.nonreporting_aggregate_upper_bound
            + stats.norm.ppf(q=quantile, loc=x.ub_mean, scale=x.ub_sd),
        )[
            aggregate + ["lb", "ub"]
        ]

        # un-residualize bounds by adding last election results
        # elementwise maximum with votes from nonreporting units to avoid adding negative vote count in nonreporting units
        # Note, gaussian interval aggregation can result in  a lower bound that is less than the votes
        # already returned in non-reporting units. If that is the case we correct by assigning the number of votes
        # already returned in nonreporting units.
        aggregate_prediction_intervals = (
            last_election.merge(modeled_bounds, how="inner", on=aggregate)
            .assign(
                predicted_lower=lambda x: np.maximum(
                    x[f"last_election_results_{estimand}"] + x.lb, aggregate_nonreporting_votes[f"results_{estimand}"]
                ),
                predicted_upper=lambda x: np.maximum(
                    x[f"last_election_results_{estimand}"] + x.ub, aggregate_nonreporting_votes[f"results_{estimand}"]
                ),
            )
            .drop(columns=f"last_election_results_{estimand}")
        )

        # add results from reporting and reporting and unexpected units to get final group bounds
        aggregate_data = (
            aggregate_votes.merge(aggregate_prediction_intervals, how="outer", on=aggregate)
            .fillna({f"results_{estimand}": 0, "predicted_lower": 0, "predicted_upper": 0})
            .assign(
                lower=lambda x: x.predicted_lower + x[f"results_{estimand}"],
                upper=lambda x: x.predicted_upper + x[f"results_{estimand}"],
            )
            .sort_values(aggregate)[aggregate + ["lower", "upper"]]
            .reset_index(drop=True)
        )

        return PredictionIntervals(aggregate_data.lower.round(decimals=0), aggregate_data.upper.round(decimals=0))
