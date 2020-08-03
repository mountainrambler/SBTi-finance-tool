import itertools
from enum import Enum
from typing import Optional, Tuple, Type, Dict

import pandas as pd
import numpy as np

from SBTi.portfolio_aggregation import PortfolioAggregation, PortfolioAggregationMethod
from .configs import TemperatureScoreConfig


class ScenarioType(Enum):
    TARGETS = 1
    APPROVED_TARGETS = 2
    HIGHEST_CONTRIBUTORS = 3
    HIGHEST_CONTRIBUTORS_APPROVED = 4

    @staticmethod
    def from_int(value) -> 'ScenarioType':
        value_map = {
            1: ScenarioType.TARGETS,
            2: ScenarioType.APPROVED_TARGETS,
            3: ScenarioType.HIGHEST_CONTRIBUTORS,
            4: ScenarioType.HIGHEST_CONTRIBUTORS_APPROVED
        }
        return value_map.get(value, ScenarioType.TARGETS)


class EngagementType(Enum):
    SET_TARGETS = 1
    SET_SBTI_TARGETS = 2

    @staticmethod
    def from_int(value) -> 'EngagementType':
        value_map = {
            0: EngagementType.SET_TARGETS,
            1: EngagementType.SET_SBTI_TARGETS,
        }
        return value_map.get(value, EngagementType.SET_TARGETS)

    @staticmethod
    def from_string(value: str) -> 'EngagementType':
        value_map = {
            'SET_TARGETS': EngagementType.SET_TARGETS,
            'SET_SBTI_TARGETS': EngagementType.SET_SBTI_TARGETS,
        }
        return value_map.get(value.upper(), EngagementType.SET_TARGETS)


class Scenario:
    scenario_type: ScenarioType
    engagement_type: EngagementType
    aggregation_method: PortfolioAggregationMethod
    grouping: Optional[list]

    @staticmethod
    def from_dict(scenario_values: dict) -> 'Scenario':
        scenario = Scenario()
        scenario.scenario_type = ScenarioType.from_int(scenario_values.get("number", -1))
        scenario.engagement_type = EngagementType.from_string(scenario_values.get("engagement_type", ""))
        scenario.aggregation_method = PortfolioAggregationMethod.from_string(scenario_values.get("engagement_type", ""))
        scenario.grouping = scenario_values.get("grouping", None)

        return scenario


class TemperatureScore(PortfolioAggregation):
    """
    This class is provides a temperature score based on the climate goals.

    :param fallback_score: The temp score if a company is not found
    :param model: The regression model to use
    :param config: A class defining the constants that are used throughout this class. This parameter is only required
                    if you'd like to overwrite a constant. This can be done by extending the TemperatureScoreConfig
                    class and overwriting one of the parameters.
    """

    def __init__(self, fallback_score: float = 3.2, model: int = 4,
                 config: Type[TemperatureScoreConfig] = TemperatureScoreConfig):
        super().__init__(config)
        self.fallback_score = fallback_score
        self.model = model
        self.c: Type[TemperatureScoreConfig] = config
        self.scenario: Optional[Scenario] = None
        self.score_cap: Optional[float] = None

        # Load the mappings from industry to SR15 goal
        self.mapping = pd.read_excel(self.c.FILE_SR15_MAPPING, header=0)
        self.regression_model = pd.read_excel(self.c.FILE_REGRESSION_MODEL_SUMMARY, header=0)
        self.regression_model = self.regression_model[self.regression_model[self.c.COLS.MODEL] == self.model]

    def get_target_mapping(self, target: pd.Series) -> Optional[str]:
        """
        Map the target onto an SR15 target (None if not available).

        :param target: The target as a row of a dataframe
        :return: The mapped SR15 target
        """
        # TODO: Use constants
        mappings = {
            self.c.VALUE_TARGET_REFERENCE_ABSOLUTE: "Emissions|Kyoto Gases",
            self.c.VALUE_TARGET_REFERENCE_INTENSITY: {
                "Revenue": "INT.emKyoto_gdp",
                "Product": "INT.emKyoto_gdp",
                "Cement": "INT.emKyoto_gdp",
                "Oil": "INT.emCO2EI_PE",
                "Steel": "INT.emKyoto_gdp",
                "Aluminum": "INT.emKyoto_gdp",
                "Power": "INT.emCO2EI_elecGen"
            }
        }

        if target[self.c.COLS.TARGET_REFERENCE_NUMBER].strip().startswith(self.c.VALUE_TARGET_REFERENCE_INTENSITY_BASE):
            mappings[self.c.VALUE_TARGET_REFERENCE_INTENSITY].get(target[self.c.COLS.INTENSITY_METRIC], None)
        else:
            return mappings.get(self.c.VALUE_TARGET_REFERENCE_ABSOLUTE, None)

    def get_annual_reduction_rate(self, target: pd.Series) -> Optional[float]:
        """
        Get the annual reduction rate (or None if not available).

        :param target: The target as a row of a dataframe
        :return: The annual reduction
        """
        if pd.isnull(target[self.c.COLS.REDUCTION_AMBITION]):
            return None

        try:
            return target[self.c.COLS.REDUCTION_AMBITION] / float(target[self.c.COLS.END_YEAR] -
                                                                  target[self.c.COLS.START_YEAR])
        except ZeroDivisionError:
            raise ValueError("Couldn't calculate the annual reduction rate because the start and target year are the "
                             "same")

    def get_regression(self, target: pd.Series) -> Tuple[Optional[float], Optional[float]]:
        """
        Get the regression parameter and intercept from the model's output.

        :param target: The target as a row of a dataframe
        :return:The regression parameter and intercept
        """
        if pd.isnull(target[self.c.COLS.SR15]):
            return None, None

        regression = self.regression_model[
            (self.regression_model[self.c.COLS.VARIABLE] == target[self.c.COLS.SR15]) &
            (self.regression_model[self.c.COLS.SLOPE] == self.c.SLOPE_MAP[target[self.c.COLS.TIME_FRAME]])]
        if len(regression) == 0:
            return None, None
        elif len(regression) > 1:
            # There should never be more than one potential mapping
            raise ValueError("There is more than one potential regression parameter for this SR15 goal.")
        else:
            return regression.iloc[0][self.c.COLS.PARAM], regression.iloc[0][self.c.COLS.INTERCEPT]

    def merge_regression(self, data):
        data[self.c.COLS.SLOPE] = data.apply(
            lambda row: self.c.SLOPE_MAP.get(row[self.c.COLS.TIME_FRAME], None),
            axis=1)
        return pd.merge(left=data, right=self.regression_model,
                        left_on=[self.c.COLS.SLOPE, self.c.COLS.SR15],
                        right_on=[self.c.COLS.SLOPE, self.c.COLS.VARIABLE],
                        how="left")

    def get_score(self, target: pd.Series) -> float:
        """
        Get the temperature score for a certain target based on the annual reduction rate and the regression parameters.

        :param target: The target as a row of a dataframe
        :return: The temperature score
        """
        if pd.isnull(target[self.c.COLS.REGRESSION_PARAM]) or pd.isnull(target[self.c.COLS.REGRESSION_INTERCEPT]) \
                or pd.isnull(target[self.c.COLS.ANNUAL_REDUCTION_RATE]):
            return self.fallback_score
        return target[self.c.COLS.REGRESSION_PARAM] * target[self.c.COLS.ANNUAL_REDUCTION_RATE] * 100 + target[
            self.c.COLS.REGRESSION_INTERCEPT]

    def get_ghc_temperature_score(self, row: pd.Series, company_data: pd.DataFrame) -> float:
        """
        Get the aggregated temperature score for a certain company based on the emissions of company.

        :param company_data: The original data, grouped by company, time frame and scope category
        :param row: The row to calculate the temperature score for (if the scope of the row isn't s1s2s3, it will return
        the original score
        :return: The aggregated temperature score for a company
        """
        if row[self.c.COLS.SCOPE_CATEGORY] != self.c.VALUE_SCOPE_CATEGORY_S1S2S3:
            return row[self.c.COLS.TEMPERATURE_SCORE]
        s1s2 = company_data.loc[(row[self.c.COLS.COMPANY_ID], row[self.c.COLS.TIME_FRAME],
                                 self.c.VALUE_SCOPE_CATEGORY_S1S2)]
        s3 = company_data.loc[(row[self.c.COLS.COMPANY_ID], row[self.c.COLS.TIME_FRAME],
                               self.c.VALUE_SCOPE_CATEGORY_S3)]

        try:
            # If the s3 emissions are less than 40 percent, we'll ignore them altogether, if not, we'll weigh them
            if s3[self.c.COLS.GHG_SCOPE3] / (s1s2[self.c.COLS.GHG_SCOPE12] + s3[self.c.COLS.GHG_SCOPE3]) < 0.4:
                return s1s2[self.c.COLS.TEMPERATURE_SCORE]
            else:
                return (s1s2[self.c.COLS.TEMPERATURE_SCORE] * s1s2[self.c.COLS.GHG_SCOPE12] +
                        s3[self.c.COLS.TEMPERATURE_SCORE] * s3[self.c.COLS.GHG_SCOPE3]) / \
                       (s1s2[self.c.COLS.GHG_SCOPE12] + s3[self.c.COLS.GHG_SCOPE3])
        except ZeroDivisionError:
            raise ValueError("The mean of the S1+S2 plus the S3 emissions is zero")

    def get_default_score(self, target: pd.Series) -> int:
        """
        Get the temperature score for a certain target based on the annual reduction rate and the regression parameters.

        :param target: The target as a row of a dataframe
        :return: The temperature score
        """
        if pd.isnull(target[self.c.COLS.REGRESSION_PARAM]) or pd.isnull(target[self.c.COLS.REGRESSION_INTERCEPT]) \
                or pd.isnull(target[self.c.COLS.ANNUAL_REDUCTION_RATE]):
            return 1
        return 0

    def _prepare_data(self, data: pd.DataFrame):
        """
        Prepare the data such that it can be used to calculate the temperature score.

        :param data: The original data set as a pandas data frame
        :return: The extended data frame
        """
        data[self.c.COLS.TARGET_REFERENCE_NUMBER] = data[self.c.COLS.TARGET_REFERENCE_NUMBER].replace(
            {np.nan: self.c.VALUE_TARGET_REFERENCE_ABSOLUTE}
        )
        data[self.c.COLS.SR15] = data.apply(lambda row: self.get_target_mapping(row), axis=1)
        data[self.c.COLS.ANNUAL_REDUCTION_RATE] = data.apply(lambda row: self.get_annual_reduction_rate(row), axis=1)
        data = self.merge_regression(data)
        data[self.c.COLS.TEMPERATURE_SCORE] = data.apply(lambda row: self.get_score(row), axis=1)
        data = self.cap_scores(data)
        return data

    def _calculate_company_score(self, data):
        """
        Calculate the combined s1s2s3 scores for all companies.

        :param data: The original data set as a pandas data frame
        :return: The data frame, with an updated s1s2s3 temperature score
        """
        # Calculate the GHC
        company_data = data[
            [self.c.COLS.COMPANY_ID, self.c.COLS.TIME_FRAME, self.c.COLS.SCOPE_CATEGORY, self.c.COLS.GHG_SCOPE12,
             self.c.COLS.GHG_SCOPE3, self.c.COLS.TEMPERATURE_SCORE]
        ].groupby([self.c.COLS.COMPANY_ID, self.c.COLS.TIME_FRAME, self.c.COLS.SCOPE_CATEGORY]).mean()

        data[self.c.COLS.TEMPERATURE_SCORE] = data.apply(
            lambda row: self.get_ghc_temperature_score(row, company_data), axis=1
        )
        return data

    def calculate(self, data: pd.DataFrame):
        """
        Calculate the temperature for a dataframe of company data.
        Required columns:
        * target_reference_number: Int *x* of Abs *x*
        * scope: The scope of the target. This should be a valid scope in the SR15 mapping
        * scope_category: The scope category, options: "s1s2", "s3", "s1s2s3"
        * base_year: The base year of the target
        * start_year: The start year of the target
        * target_year: The year when the target should be achieved
        * time_frame: The time frame of the target (short, mid, long) -> This field is calculated by the target
            valuation protocol.
        * reduction_from_base_year: Targeted reduction in emissions from the base year
        * emissions_in_scope: Company emissions in the target's scope at start of the base year
        * achieved_reduction: The emission reduction that has already been achieved
        * industry: The industry the company is working in. This should be a valid industry in the SR15 mapping. If not
            it will be converted to "Others" (or whichever value is set in the config as the default
        * s1s2_emissions: Total company emissions in the S1 + S2 scope
        * s3_emissions: Total company emissions in the S3 scope
        * market_cap: Market capitalization of the company. Only required to use the MOTS portfolio aggregation.
        * investment_value: The investment value of the investment in this company. Only required to use the MOTS, EOTS,
            ECOTS and AOTS portfolio aggregation.
        * company_enterprise_value: The enterprise value of the company. Only required to use the EOTS portfolio
            aggregation.
        * company_ev_plus_cash: The enterprise value of the company plus cash. Only required to use the ECOTS portfolio
            aggregation.
        * company_total_assets: The total assets of the company. Only required to use the AOTS portfolio aggregation.
        * company_revenue: The revenue of the company. Only required to use the ROTS portfolio aggregation.

        :param extra_columns: A list of user defined extra, company related, columns
        :param data:
        :return: A data frame containing all relevant information for the targets and companies
        """
        data = self._prepare_data(data)
        data = self._calculate_company_score(data)
        return data

    def aggregate_scores(self, data: pd.DataFrame, portfolio_aggregation_method: PortfolioAggregationMethod,
                         grouping: Optional[list] = None):
        """
        Aggregate scores to create a portfolio score per time_frame (short, mid, long).

        :param data: The results of the calculate method
        :param portfolio_aggregation_method: PortfolioAggregationMethod: The aggregation method to use
        :param grouping: The grouping to use
        :return: A weighted temperature score for the portfolio
        """
        portfolio_scores: Dict = {
            time_frame: {scope: {} for scope in data[self.c.COLS.SCOPE_CATEGORY].unique()}
            for time_frame in data[self.c.COLS.TIME_FRAME].unique()}

        for time_frame, scope in itertools.product(data[self.c.COLS.TIME_FRAME].unique(),
                                                   data[self.c.COLS.SCOPE_CATEGORY].unique()):
            filtered_data = data[(data[self.c.COLS.TIME_FRAME] == time_frame) & (
                    data[self.c.COLS.SCOPE_CATEGORY] == scope)].copy()

            if not filtered_data.empty:
                weighted_scores = self._calculate_aggregate_score(filtered_data, self.c.COLS.TEMPERATURE_SCORE,
                                                                  self.c.COLS.WEIGHTED_TEMPERATURE_SCORE,
                                                                  portfolio_aggregation_method)
                portfolio_scores[time_frame][scope]["all"] = {}
                portfolio_scores[time_frame][scope]["all"]["score"] = round(weighted_scores.sum(), 4)
                filtered_data[self.c.COLS.CONTRIBUTION_RELATIVE] = weighted_scores / (
                        weighted_scores.sum() / 100).round(2)
                filtered_data[self.c.COLS.CONTRIBUTION] = weighted_scores
                portfolio_scores[time_frame][scope]["all"]["contributions"] = filtered_data \
                    .sort_values(self.c.COLS.CONTRIBUTION_RELATIVE, ascending=False)[
                    self.c.CONTRIBUTION_COLUMNS].to_dict(orient="records")

                # If there are grouping column(s) we'll group in pandas and pass the results to the aggregation
                if grouping is not None and len(grouping) > 0:
                    grouped_data = filtered_data.groupby(grouping)
                    for group_name, group in grouped_data:
                        group_data = group.copy()
                        weighted_scores = self._calculate_aggregate_score(group_data, self.c.COLS.TEMPERATURE_SCORE,
                                                                          self.c.COLS.WEIGHTED_TEMPERATURE_SCORE,
                                                                          portfolio_aggregation_method)
                        group_name_joined = group_name if type(group_name) == str else "-".join(group_name)
                        group_data[self.c.COLS.CONTRIBUTION_RELATIVE] = weighted_scores / (weighted_scores.sum() / 100)
                        group_data[self.c.COLS.CONTRIBUTION] = weighted_scores
                        portfolio_scores[time_frame][scope][group_name_joined] = {}
                        portfolio_scores[time_frame][scope][group_name_joined]["score"] = weighted_scores.sum().round(2)
                        portfolio_scores[time_frame][scope][group_name_joined]["contributions"] = \
                            group_data.sort_values(self.c.COLS.CONTRIBUTION_RELATIVE, ascending=False)[
                                self.c.CONTRIBUTION_COLUMNS].to_dict(orient="records")
            else:
                portfolio_scores[time_frame][scope] = None

        return portfolio_scores

    def _calculate_company_unique_sum(self, data: pd.DataFrame, col: str) -> float:
        """
        Given a data set, calculate a sum which is unique at the company level (such that each field is counted once
        per company).

        :param data: The data set
        :param col: The column name
        :return:
        """
        return data[[self.c.COLS.COMPANY_NAME, col]].drop_duplicates()[col].sum()

    def _calculate_scope_weight(self, company_data: pd.DataFrame, scope: str) -> float:
        """
        Calculate the weight that a certain scope has in the attribution calculation (which calculate how much of the
        total score is dependent on the default score).

        :param company_data: A data set which only contains company specific information
        :param scope: The scope category for which the weight should be calculated.
        :return:
        """
        # TODO: You still have three options here (three time frames), which each have a different target
        ds_s1s2 = company_data[company_data[self.c.COLS.SCOPE_CATEGORY] == self.c.VALUE_SCOPE_CATEGORY_S1S2][
            self.c.TEMPERATURE_RESULTS].unique()[0]
        ds_s3 = company_data[company_data[self.c.COLS.SCOPE_CATEGORY] == self.c.VALUE_SCOPE_CATEGORY_S3][
            self.c.TEMPERATURE_RESULTS].unique()[0]

        if scope == self.c.VALUE_SCOPE_CATEGORY_S1S2:
            scope_weight = ds_s1s2
        elif scope == self.c.VALUE_SCOPE_CATEGORY_S3:
            scope_weight = ds_s3
        else:
            s1s2_emissions = company_data.iloc[1][self.c.COLS.GHG_SCOPE12]
            s3_emissions = company_data.iloc[1][self.c.COLS.GHG_SCOPE3]
            scope_weight = (ds_s1s2 * (s1s2_emissions / (s1s2_emissions + s3_emissions)) +
                            ds_s3 * (s3_emissions / (s1s2_emissions + s3_emissions)))
        return scope_weight

    # TODO: Type hinting
    def temperature_score_influence_percentage(self, data, aggregation_method):
        """
        Determines the percentage of the temperature score is covered by target and default score

        Required columns:
        * target_reference_number: Int *x* of Abs *x*
        * scope: The scope of the target. This should be a valid scope in the SR15 mapping
        * scope_category: The scope category, options: "s1s2", "s3", "s1s2s3"
        * base_year: The base year of the target
        * start_year: The start year of the target
        * target_year: The year when the target should be achieved
        * time_frame: The time frame of the target (short, mid, long) -> This field is calculated by the target
            valuation protocol.
        * reduction_from_base_year: Targeted reduction in emissions from the base year
        * emissions_in_scope: Company emissions in the target's scope at start of the base year
        * achieved_reduction: The emission reduction that has already been achieved
        * industry: The industry the company is working in. This should be a valid industry in the SR15 mapping. If not
            it will be converted to "Others" (or whichever value is set in the config as the default
        * s1s2_emissions: Total company emissions in the S1 + S2 scope
        * s3_emissions: Total company emissions in the S3 scope
        * market_cap: Market capitalization of the company. Only required to use the MOTS portfolio aggregation.
        * investment_value: The investment value of the investment in this company. Only required to use the MOTS, EOTS,
            ECOTS, AOTS and ROTS portfolio aggregation.
        * company_enterprise_value: The enterprise value of the company. Only required to use the EOTS portfolio
            aggregation.
        * company_ev_plus_cash: The enterprise value of the company plus cash. Only required to use the ECOTS portfolio
            aggregation.
        * company_total_assets: The total assets of the company. Only required to use the AOTS portfolio aggregation.

        :param data: output from the target_valuation_protocol

        :return: A dataframe containing the percentage contributed by the default and target score for all three timeframes
        """
        # TODO: Check if this hasn't already been done
        data[self.c.COLS.SR15] = data.apply(lambda row: self.get_target_mapping(row), axis=1)
        data[self.c.COLS.ANNUAL_REDUCTION_RATE] = data.apply(lambda row: self.get_annual_reduction_rate(row), axis=1)
        data[self.c.COLS.REGRESSION_PARAM], data[self.c.COLS.REGRESSION_INTERCEPT] = zip(
            *data.apply(lambda row: self.get_regression(row), axis=1))

        data[self.c.TEMPERATURE_RESULTS] = data.apply(lambda row: self.get_default_score(row), axis=1)

        # TODO: Why doesn't this use an enum
        emission_based_methods = {
            "MOTS": self.c.COLS.MARKET_CAP,
            "EOTS": self.c.COLS.COMPANY_ENTERPRISE_VALUE,
            "ECOTS": self.c.COLS.COMPANY_EV_PLUS_CASH,
            "AOTS": self.c.COLS.COMPANY_TOTAL_ASSETS,
            "ROTS": self.c.COLS.COMPANY_REVENUE,
        }

        value_column = emission_based_methods.get(aggregation_method)
        total_investment, portfolio_emissions = 0, 0
        if aggregation_method == "WATS":
            total_investment = self._calculate_company_unique_sum(data, self.c.COLS.INVESTMENT_VALUE)
        elif aggregation_method == "TETS":
            portfolio_emissions = self._calculate_company_unique_sum(data, self.c.COLS.GHG_SCOPE12) + \
                                  self._calculate_company_unique_sum(data, self.c.COLS.GHG_SCOPE3)
        elif aggregation_method == "ECOTS":
            data[self.c.COLS.COMPANY_EV_PLUS_CASH] = data[self.c.COLS.COMPANY_ENTERPRISE_VALUE] + \
                                                     data[self.c.COLS.CASH_EQUIVALENTS]

        # Calculate the total owned emissions of all companies
        owned_emissions = 0
        if value_column:
            try:
                data[self.c.COLS.OWNED_EMISSIONS] = data.apply(
                    lambda row: ((row[self.c.COLS.INVESTMENT_VALUE] / row[value_column]) * (
                            row[self.c.COLS.GHG_SCOPE12] + row[self.c.COLS.GHG_SCOPE3])),
                    axis=1
                )
                owned_emissions = self._calculate_company_unique_sum(data, self.c.COLS.OWNED_EMISSIONS)
            except ZeroDivisionError:
                raise ValueError("To calculate the aggregation, the {} column may not be zero".format(value_column))

        company_temp_contribution = {
            time_frame: {
                scope: {company: 0 for company in data[self.c.COLS.COMPANY_NAME].unique()} for scope in
                self.c.VALUE_SCOPE_CATEGORIES
            } for time_frame in data[self.c.COLS.TIME_FRAME].unique()
        }

        time_frame_dictionary = {time_frame: {} for time_frame in data[self.c.COLS.TIME_FRAME].unique()}

        for time_frame, scope, company in itertools.product(*[data[self.c.COLS.TIME_FRAME].unique(),
                                                              self.c.VALUE_SCOPE_CATEGORIES,
                                                              data[self.c.COLS.COMPANY_NAME].unique()]):
            company_data = data[
                (data[self.c.COLS.COMPANY_NAME] == company) & (data[self.c.COLS.TIME_FRAME] == time_frame)]
            scope_weight = self._calculate_scope_weight(company_data, scope)

            company_emissions = company_data[self.c.COLS.GHG_SCOPE12].iloc[0] + \
                                company_data[self.c.COLS.GHG_SCOPE3].iloc[0]

            if aggregation_method == 'WATS':
                value = (company_data.iloc[1][self.c.INVESTMENT_VALUE] / total_investment) * scope_weight
            elif aggregation_method == 'TETS':
                value = company_emissions / portfolio_emissions * scope_weight
            else:
                # The other methods only differ in the way the company is valued.
                value = company_data[self.c.COLS.INVESTMENT_VALUE].iloc[0] / company_data[value_column].iloc[0] * \
                        company_emissions / owned_emissions * scope_weight

            company_temp_contribution[time_frame][scope][company] = value

        for time_frame, scope in itertools.product(*[data[self.c.COLS.TIME_FRAME].unique(),
                                                     self.c.VALUE_SCOPE_CATEGORIES]):
            time_frame_dictionary[time_frame][scope] = round(
                sum(company_temp_contribution[time_frame][scope].values()), 3)

        return time_frame_dictionary

    def columns_percentage_distribution(self, data, columns):
        '''
        Percentage distribution of specific column or columns

        :param data: output from the target_valuation_protocol
        :param columns: specified column names the client would like to have a percentage distribution
        :return: percentage distribution of specified columns
        '''

        data = data[columns].fillna('unknown')
        if columns is None:
            return None
        elif len(columns) == 1:
            percentage_distribution = round((data.groupby(columns[0]).size() / data[columns[0]].count()) * 100, 2)
            return percentage_distribution.to_dict()
        elif len(columns) > 1:
            percentage_distribution = round((data.groupby(columns).size() / data[columns[0]].count()) * 100, 2)
            percentage_distribution = percentage_distribution.to_dict()

            percentage_distribution_copy = percentage_distribution.copy()
            # Modifies the original key name (tuple) into string representation
            for key, value in percentage_distribution_copy.items():
                key_combined = key if type(key) == str else "-".join(key)
                percentage_distribution[key_combined] = percentage_distribution[key]
                del percentage_distribution[key]
            return percentage_distribution

    def set_scenario(self, scenario: Scenario):
        # TODO: Enums, docstrings, constants
        self.scenario = scenario
        # Scenario 1: Engage companies to set targets
        if self.scenario.scenario_type == ScenarioType.TARGETS:
            self.fallback_score = 2.0
        # Scenario 2: Engage companies to validate targets by SBTi
        elif self.scenario.scenario_type == ScenarioType.APPROVED_TARGETS:
            self.score_cap = 1.75
        # Scenario 3: Engaging the highest contributors (top 10) to set (better) targets
        elif self.scenario.scenario_type == ScenarioType.HIGHEST_CONTRIBUTORS or \
                self.scenario.scenario_type == ScenarioType.HIGHEST_CONTRIBUTORS_APPROVED:
            if self.scenario.engagement_type == EngagementType.SET_TARGETS:
                self.score_cap = 2.0
            elif self.scenario.engagement_type == EngagementType.SET_SBTI_TARGETS:
                self.score_cap = 1.75

    def cap_scores(self, scores: pd.DataFrame):
        # TODO: Enums, docstrings, constants
        if self.scenario is None:
            return scores
        if self.scenario.scenario_type == ScenarioType.APPROVED_TARGETS:
            score_based_on_target = ~pd.isnull(scores[self.c.COLS.TARGET_REFERENCE_NUMBER])
            scores.loc[score_based_on_target, self.c.COLS.TEMPERATURE_SCORE] = \
                scores.loc[score_based_on_target, self.c.COLS.TEMPERATURE_SCORE].apply(lambda x: min(x, self.score_cap))
        elif self.scenario.scenario_type == ScenarioType.HIGHEST_CONTRIBUTORS:
            # Cap scores of 10 highest contributors per time frame-scope combination
            # TODO: Should this actually be per time-frame/scope combi? Aren't you engaging the company as a whole?
            aggregations = self.aggregate_scores(scores, self.scenario.aggregation_method, self.scenario.grouping)
            for time_frame in self.c.VALUE_TIME_FRAMES:
                for scope in scores[self.c.COLS.SCOPE_CATEGORY].unique():
                    number_top_contributors = min(10, len(aggregations[time_frame][scope]['all']['contributions']))
                    for contributor in range(number_top_contributors):
                        company_name = aggregations[time_frame][scope]['all']['contributions'][contributor][
                            self.c.COLS.COMPANY_NAME]
                        company_mask = ((scores[self.c.COLS.COMPANY_NAME] == company_name) &
                                        (scores[self.c.COLS.SCOPE_CATEGORY] == scope) &
                                        (scores[self.c.COLS.TIME_FRAME] == time_frame))
                        scores.loc[company_mask, self.c.COLS.TEMPERATURE_SCORE] = \
                            scores.loc[company_mask, self.c.COLS.TEMPERATURE_SCORE].apply(
                                lambda x: min(x, self.score_cap))
        elif self.scenario.scenario_type == ScenarioType.HIGHEST_CONTRIBUTORS_APPROVED:
            scores[self.c.COLS.ENGAGEMENT_TARGET] = scores[self.c.COLS.ENGAGEMENT_TARGET] == True
            score_based_on_target = scores[self.c.COLS.ENGAGEMENT_TARGET]
            scores.loc[score_based_on_target, self.c.COLS.TEMPERATURE_SCORE] = \
                scores.loc[score_based_on_target, self.c.COLS.TEMPERATURE_SCORE].apply(lambda x: min(x, self.score_cap))
        return scores

    def anonymize_data_dump(self, scores):
        '''
        Anonymizes scores for raw data output
        '''
        scores.drop(columns=[self.c.COLS.COMPANY_ISIC, self.c.COLS.COMPANY_ID], inplace=True)
        for index, company_name in enumerate(scores[self.c.COLS.COMPANY_NAME].unique()):
            scores.loc[scores[self.c.COLS.COMPANY_NAME] == company_name, self.c.COLS.COMPANY_NAME] = 'Company' + str(
                index + 1)
        return scores

    def merge_percentage_coverage_to_aggregations(self, aggregations: Dict, temperature_percentage_coverage: Dict):
        """Iterates over two dictionaries and ads keys from second dictionary to the first.
        :param temperature_percentage_coverage: first 'main' dictionary where keys should be added
        :type temperature_percentage_coverage: dict
        :param aggregations: second dictionary wherefrom key-value pairs are added to first dictionary
        :type aggregations: dict
        :rtype: aggregations, dict
        :return: aggregations
        """
        for time_frame in [self.c.TIME_FRAME_SHORT, self.c.TIME_FRAME_MID, self.c.TIME_FRAME_LONG]:
            for scope in self.c.VALUE_SCOPE_CATEGORIES:
                if aggregations.get(time_frame) and aggregations[time_frame].get(scope):
                    aggregations[time_frame][scope]['influence_percentage'] = temperature_percentage_coverage[
                                                                                  time_frame][scope] * 100
        return aggregations
