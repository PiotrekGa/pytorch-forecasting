"""
Encoders for encoding categorical variables and scaling continuous data.
"""
from typing import Dict, Iterable, List, Tuple, Union
import warnings

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin
import torch
import torch.nn.functional as F


class NaNLabelEncoder(BaseEstimator, TransformerMixin):
    """
    Labelencoder that can optionally always encode nan and unknown classes (in transform) as class ``0``
    """

    def __init__(self, add_nan: bool = False, warn: bool = True):
        """
        init NaNLabelEncoder

        Args:
            add_nan: if to force encoding of nan at 0
            warn: if to warn if additional nans are added because items are unknown
        """
        self.add_nan = add_nan
        self.warn = warn
        super().__init__()

    def fit_transform(self, y: pd.Series) -> np.ndarray:
        """
        Fit and transform data.

        Args:
            y (pd.Series): input data

        Returns:
            np.ndarray: encoded data
        """
        if self.add_nan:
            self.fit(y)
            return self.transform(y)
        return super().transform(y)

    @staticmethod
    def is_numeric(y: pd.Series) -> bool:
        """
        Determine if series is numeric or not. Will also return True if series is a categorical type with
        underlying integers.

        Args:
            y (pd.Series): series for which to carry out assessment

        Returns:
            bool: True if series is numeric
        """
        return y.dtype.kind in "bcif" or (isinstance(y, pd.CategoricalDtype) and y.cat.categories.dtype.kind in "bcif")

    def fit(self, y: pd.Series):
        """
        Fit transformer

        Args:
            y (pd.Series): input data to fit on

        Returns:
            NaNLabelEncoder: self
        """
        if self.add_nan:
            if self.is_numeric(y):
                nan = np.nan
            else:
                nan = "nan"
            self.classes_ = {nan: 0}
            for idx, val in enumerate(np.unique(y)):
                self.classes_[val] = idx + 1
        else:
            self.classes_ = {val: idx for idx, val in enumerate(np.unique(y))}
        self.classes_vector_ = np.array(list(self.classes_.keys()))
        return self

    def transform(self, y: Iterable) -> Union[torch.Tensor, np.ndarray]:
        """
        Encode iterable with integers.

        Args:
            y (Iterable): iterable to encode

        Returns:
            Union[torch.Tensor, np.ndarray]: returns encoded data as torch tensor or numpy array depending on input type
        """
        if self.add_nan:
            if self.warn:
                cond = ~np.isin(y, self.classes_)
                if cond.any():
                    warnings.warn(
                        f"Found {np.unique(np.asarray(y)[cond]).size} unknown classes which were set to NaN",
                        UserWarning,
                    )

            encoded = [self.classes_.get(v, 0) for v in y]

        else:
            encoded = [self.classes_[v] for v in y]

        if isinstance(y, torch.Tensor):
            encoded = torch.tensor(encoded, dtype=torch.long, device=y.device)
        else:
            encoded = np.array(encoded)
        return encoded

    def inverse_transform(self, y: Union[torch.Tensor, np.ndarray]) -> np.ndarray:
        """
        Decode data, i.e. transform from integers to labels.

        Args:
            y (Union[torch.Tensor, np.ndarray]): encoded data

        Raises:
            KeyError: if unknown elements should be decoded

        Returns:
            np.ndarray: decoded data
        """
        if y.max() >= len(self.classes_vector_):
            raise KeyError("New unknown values detected")

        # decode
        decoded = self.classes_vector_[y]
        return decoded

    def __call__(self, data: (Dict[str, torch.Tensor])) -> torch.Tensor:
        """
        Extract prediction from network output. Does not map back to input
        categories as this would require a numpy tensor without grad-abilities.

        Args:
            data (Dict[str, torch.Tensor]): Dictionary with entries
                * prediction: data to de-scale

        Returns:
            torch.Tensor: prediction
        """
        return data["prediction"]


class TorchNormalizer(BaseEstimator, TransformerMixin):
    """
    Basic target transformer that can be fit also on torch tensors.
    """

    def __init__(
        self,
        method: str = "standard",
        center: bool = True,
        log_scale: Union[bool, float] = False,
        log_zero_value: float = -np.inf,
        coerce_positive: Union[float, bool] = None,
        eps: float = 1e-8,
    ):
        """
        Initialize

        Args:
            method (str, optional): method to rescale series. Either "standard" (standard scaling) or "robust"
                (scale using quantiles 0.25-0.75). Defaults to "standard".
            center (bool, optional): If to center the output to zero. Defaults to True.
            log_scale (bool, optional): If to take log of values. Defaults to False. Defaults to False.
            log_zero_value (float, optional): Value to map 0 to for ``log_scale=True`` or in softplus. Defaults to -inf.
            coerce_positive (Union[bool, float, str], optional): If to coerce output to positive. Valid values:
                * None, i.e. is automatically determined and might change to True if all values are >= 0 (Default).
                * True, i.e. output is clamped at 0.
                * False, i.e. values are not coerced
                * float, i.e. softmax is applied with beta = coerce_positive.
            eps (float, optional): Number for numerical stability of calcualtions.
                Defaults to 1e-8. For count data, 1.0 is recommended.
        """
        self.method = method
        assert method in ["standard", "robust"], f"method has invalid value {method}"
        self.center = center
        self.eps = eps

        # set log scale
        self.log_zero_value = np.exp(log_zero_value)
        self.log_scale = log_scale

        # check if coerce positive should be determined automatically
        if coerce_positive is None:
            if log_scale:
                coerce_positive = False
        else:
            assert not (self.log_scale and coerce_positive), (
                "log scale means that output is transformed to a positive number by default while coercing positive"
                " will apply softmax function - decide for either one or the other"
            )
        self.coerce_positive = coerce_positive

    def get_parameters(self, *args, **kwargs) -> torch.Tensor:
        """
        Returns parameters that were used for encoding.

        Returns:
            torch.Tensor: First element is center of data and second is scale
        """
        return torch.stack([torch.as_tensor(self.center_), torch.as_tensor(self.scale_)], dim=-1)

    def _preprocess_y(self, y: Union[pd.Series, np.ndarray, torch.Tensor]) -> Union[np.ndarray, torch.Tensor]:
        """
        Preprocess input data (e.g. take log).

        Can set coerce positive to a value if it was set to None and log_scale to False.

        Returns:
            Union[np.ndarray, torch.Tensor]: return rescaled series with type depending on input type
        """
        if self.log_scale:
            if isinstance(y, torch.Tensor):
                y = torch.log(y + self.log_zero_value + self.eps)
            else:
                y = np.log(y + self.log_zero_value + self.eps)
        return y

    def fit(self, y: Union[pd.Series, np.ndarray, torch.Tensor]):
        """
        Fit transformer, i.e. determine center and scale of data

        Args:
            y (Union[pd.Series, np.ndarray, torch.Tensor]): input data

        Returns:
            TorchNormalizer: self
        """
        if self.coerce_positive is None and not self.log_scale:
            self.coerce_positive = (y >= 0).all()
        y = self._preprocess_y(y)

        if self.method == "standard":
            if isinstance(y, torch.Tensor):
                self.center_ = torch.mean(y, dim=-1) + self.eps
                self.scale_ = torch.std(y, dim=-1) + self.eps
            elif isinstance(y, np.ndarray):
                self.center_ = np.mean(y, axis=-1) + self.eps
                self.scale_ = np.std(y, axis=-1) + self.eps
            else:
                self.center_ = np.mean(y) + self.eps
                self.scale_ = np.std(y) + self.eps

        elif self.method == "robust":
            if isinstance(y, torch.Tensor):
                self.center_ = torch.median(y, dim=-1).values + self.eps
                q_75 = y.kthvalue(int(len(y) * 0.75), dim=-1).values
                q_25 = y.kthvalue(int(len(y) * 0.25), dim=-1).values
            elif isinstance(y, np.ndarray):
                self.center_ = np.median(y, axis=-1) + self.eps
                q_75 = np.percentiley(y, 75, axis=-1)
                q_25 = np.percentiley(y, 25, axis=-1)
            else:
                self.center_ = np.median(y) + self.eps
                q_75 = np.percentiley(y, 75)
                q_25 = np.percentiley(y, 25)
            self.scale_ = (q_75 - q_25) / 2.0 + self.eps
        if not self.center:
            self.scale_ = self.center_
            if isinstance(y, torch.Tensor):
                self.center_ = torch.zeros_like(self.center_)
            else:
                self.center_ = np.zeros_like(self.center_)
        return self

    def transform(
        self,
        y: Union[pd.Series, np.ndarray, torch.Tensor],
        return_norm: bool = False,
        target_scale: torch.Tensor = None,
    ) -> Union[Tuple[Union[np.ndarray, torch.Tensor], np.ndarray], Union[np.ndarray, torch.Tensor]]:
        """
        Rescale data.

        Args:
            y (Union[pd.Series, np.ndarray, torch.Tensor]): input data
            return_norm (bool, optional): [description]. Defaults to False.
            target_scale (torch.Tensor): target scale to use instead of fitted center and scale

        Returns:
            Union[Tuple[Union[np.ndarray, torch.Tensor], np.ndarray], Union[np.ndarray, torch.Tensor]]: rescaled
                data with type depending on input type. returns second element if ``return_norm=True``
        """
        y = self._preprocess_y(y)
        # get center and scale
        if target_scale is None:
            target_scale = self.get_parameters().numpy()[None, :]
        center = target_scale[..., 0]
        scale = target_scale[..., 1]
        if y.ndim > center.ndim:  # multiple batches -> expand size
            center = center.view(*center.size(), *(1,) * (y.ndim - center.ndim))
            scale = scale.view(*scale.size(), *(1,) * (y.ndim - scale.ndim))

        # transform
        y = (y - center) / scale

        # return with center and scale or without
        if return_norm:
            return y, target_scale
        else:
            return y

    def inverse_transform(self, y: torch.Tensor) -> torch.Tensor:
        """
        Inverse scale.

        Args:
            y (torch.Tensor): scaled data

        Returns:
            torch.Tensor: de-scaled data
        """
        return self(dict(prediction=y, target_scale=self.get_parameters().unsqueeze(0)))

    def __call__(self, data: Dict[str, torch.Tensor]) -> torch.Tensor:
        """
        Inverse transformation but with network output as input.

        Args:
            data (Dict[str, torch.Tensor]): Dictionary with entries
                * prediction: data to de-scale
                * target_scale: center and scale of data
            scale_only (bool): if to only scale prediction and not center it (even if `self.center is True`).
                Defaults to False.

        Returns:
            torch.Tensor: de-scaled data
        """
        # inverse transformation with tensors
        norm = data["target_scale"]

        # use correct shape for norm
        if data["prediction"].ndim > norm.ndim:
            norm = norm.unsqueeze(-1)

        # transform
        y_normed = data["prediction"] * norm[:, 1, None] + norm[:, 0, None]

        if self.log_scale:
            y_normed = (y_normed.exp() - self.log_zero_value).clamp_min(0.0)
        elif isinstance(self.coerce_positive, bool) and self.coerce_positive:
            y_normed = y_normed.clamp_min(0.0)
        elif isinstance(self.coerce_positive, float):
            y_normed = F.softplus(y_normed, beta=float(self.coerce_positive))

        # return correct shape
        if data["prediction"].ndim == 1 and y_normed.ndim > 1:
            y_normed = y_normed.squeeze(0)
        return y_normed


class EncoderNormalizer(TorchNormalizer):
    """
    Special Normalizer that is fit on each encoding sequence.

    If passed as target normalizer, this transformer will be fitted on each encoder sequence separately.
    """

    pass


class GroupNormalizer(TorchNormalizer):
    """
    Normalizer that scales by groups.

    For each group a scaler is fitted and applied. This scaler can be used as target normalizer or
    also to normalize any other variable.
    """

    # todo: allow window (exp weighted), different methods such as quantile for robust scaling
    def __init__(
        self,
        method: str = "standard",
        groups: List[str] = [],
        center: bool = True,
        scale_by_group: bool = False,
        log_scale: Union[bool, float] = False,
        log_zero_value: float = 0.0,
        coerce_positive: Union[float, bool] = None,
        eps: float = 1e-8,
    ):
        """
        Group normalizer to normalize a given entry by groups. Can be used as target normalizer.

        Args:
            method (str, optional): method to rescale series. Either "standard" (standard scaling) or "robust"
                (scale using quantiles 0.25-0.75). Defaults to "standard".
            groups (List[str], optional): Group names to normalize by. Defaults to [].
            center (bool, optional): If to center the output to zero. Defaults to True.
            scale_by_group (bool, optional): If to scale the output by group, i.e. norm is calculated as
                ``(group1_norm * group2_norm * ...) ^ (1 / n_groups)``. Defaults to False.
            log_scale (bool, optional): If to take log of values. Defaults to False. Defaults to False.
            log_zero_value (float, optional): Value to map 0 to for ``log_scale=True`` or in softplus. Defaults to 0.0
            coerce_positive (Union[bool, float, str], optional): If to coerce output to positive. Valid values:
                * None, i.e. is automatically determined and might change to True if all values are >= 0 (Default).
                * True, i.e. output is clamped at 0.
                * False, i.e. values are not coerced
                * float, i.e. softmax is applied with beta = coerce_positive.
            eps (float, optional): Number for numerical stability of calcualtions.
                Defaults to 1e-8. For count data, 1.0 is recommended.
        """
        self.groups = groups
        self.scale_by_group = scale_by_group
        super().__init__(
            method=method,
            center=center,
            log_scale=log_scale,
            log_zero_value=log_zero_value,
            coerce_positive=coerce_positive,
            eps=eps,
        )

    def fit(self, y: pd.Series, X: pd.DataFrame):
        """
        Determine scales for each group

        Args:
            y (pd.Series): input data
            X (pd.DataFrame): dataframe with columns for each group defined in ``groups`` parameter.

        Returns:
            self
        """
        if self.coerce_positive is None and not self.log_scale:
            self.coerce_positive = (y >= 0).all()
        y = self._preprocess_y(y)
        if len(self.groups) == 0:
            assert not self.scale_by_group, "No groups are defined, i.e. `scale_by_group=[]`"
            if self.method == "standard":
                self.norm_ = [np.mean(y) + self.eps, np.std(y) + self.eps]  # center and scale
            else:
                quantiles = np.quantile(y, [0.25, 0.5, 0.75])
                self.norm_ = [
                    quantiles[1] + self.eps,
                    (quantiles[2] - quantiles[0]) / 2.0 + self.eps,
                ]  # center and scale
            if not self.center:
                self.norm_[1] = self.norm_[0]
                self.norm_[0] = 0.0

        elif self.scale_by_group:
            if self.method == "standard":
                self.norm_ = {
                    g: X[[g]]
                    .assign(y=y)
                    .groupby(g, observed=True)
                    .agg(center=("y", "mean"), scale=("y", "std"))
                    .assign(center=lambda x: x["center"] + self.eps, scale=lambda x: x.scale + self.eps)
                    for g in self.groups
                }
            else:
                self.norm_ = {
                    g: X[[g]]
                    .assign(y=y)
                    .groupby(g, observed=True)
                    .y.quantile([0.25, 0.5, 0.75])
                    .unstack(-1)
                    .assign(
                        center=lambda x: x[0.5] + self.eps,
                        scale=lambda x: (x[0.75] - x[0.25]) / 2.0 + self.eps,
                    )[["center", "scale"]]
                    for g in self.groups
                }
            # calculate missings
            if not self.center:  # swap center and scale

                def swap_parameters(norm):
                    norm["scale"] = norm["center"]
                    norm["center"] = 0.0
                    return norm

                self.norm = {g: swap_parameters(norm) for g, norm in self.norm_.items()}
            self.missing_ = {group: scales.median().to_dict() for group, scales in self.norm_.items()}

        else:
            if self.method == "standard":
                self.norm_ = (
                    X[self.groups]
                    .assign(y=y)
                    .groupby(self.groups, observed=True)
                    .agg(center=("y", "mean"), scale=("y", "std"))
                    .assign(center=lambda x: x["center"] + self.eps, scale=lambda x: x.scale + self.eps)
                )
            else:
                self.norm_ = (
                    X[self.groups]
                    .assign(y=y)
                    .groupby(self.groups, observed=True)
                    .y.quantile([0.25, 0.5, 0.75])
                    .unstack(-1)
                    .assign(
                        center=lambda x: x[0.5] + self.eps,
                        scale=lambda x: (x[0.75] - x[0.25]) / 2.0 + self.eps,
                    )[["center", "scale"]]
                )
            if not self.center:  # swap center and scale
                self.norm_["scale"] = self.norm_["center"]
                self.norm_["center"] = 0.0
            self.missing_ = self.norm_.median().to_dict()
        return self

    @property
    def names(self) -> List[str]:
        """
        Names of determined scales.

        Returns:
            List[str]: list of names
        """
        return ["center", "scale"]

    def fit_transform(
        self, y: pd.Series, X: pd.DataFrame, return_norm: bool = False
    ) -> Union[np.ndarray, Tuple[np.ndarray, np.ndarray]]:
        """
        Fit normalizer and scale input data.

        Args:
            y (pd.Series): data to scale
            X (pd.DataFrame): dataframe with ``groups`` columns
            return_norm (bool, optional): If to return . Defaults to False.

        Returns:
            Union[np.ndarray, Tuple[np.ndarray, np.ndarray]]: Scaled data, if ``return_norm=True``, returns also scales
                as second element
        """
        return self.fit(y, X).transform(y, X, return_norm=return_norm)

    def inverse_transform(self, y: pd.Series, X: pd.DataFrame):
        """
        Rescaling data to original scale - not implemented - call class with target scale instead.
        """
        raise NotImplementedError()

    def transform(
        self, y: pd.Series, X: pd.DataFrame = None, return_norm: bool = False, target_scale: torch.Tensor = None
    ) -> Union[np.ndarray, Tuple[np.ndarray, np.ndarray]]:
        """
        Scale input data.

        Args:
            y (pd.Series): data to scale
            X (pd.DataFrame): dataframe with ``groups`` columns
            return_norm (bool, optional): If to return . Defaults to False.
            target_scale (torch.Tensor): target scale to use instead of fitted center and scale

        Returns:
            Union[np.ndarray, Tuple[np.ndarray, np.ndarray]]: Scaled data, if ``return_norm=True``, returns also scales
                as second element
        """
        if target_scale is None:
            assert X is not None, "either target_scale or X has to be passed"
            target_scale = self.get_norm(X)
        return super().transform(y=y, return_norm=return_norm, target_scale=target_scale)

    def get_parameters(self, groups: Union[torch.Tensor, list, tuple], group_names: List[str] = None) -> np.ndarray:
        """
        Get fitted scaling parameters for a given group.

        Args:
            groups (Union[torch.Tensor, list, tuple]): group ids for which to get parameters
            group_names (List[str], optional): Names of groups corresponding to positions
                in ``groups``. Defaults to None, i.e. the instance attribute ``groups``.

        Returns:
            np.ndarray: parameters used for scaling
        """
        if isinstance(groups, torch.Tensor):
            groups = groups.tolist()
        if isinstance(groups, list):
            groups = tuple(groups)
        if group_names is None:
            group_names = self.groups
        else:
            # filter group names
            group_names = [name for name in group_names if name in self.groups]
        assert len(group_names) == len(self.groups), "Passed groups and fitted do not match"

        if len(self.groups) == 0:
            params = np.asarray(self.norm_).squeeze()
        elif self.scale_by_group:
            norm = np.array([1.0, 1.0])
            for group, group_name in zip(groups, group_names):
                try:
                    norm = norm * self.norm_[group_name].loc[group].to_numpy()
                except KeyError:
                    norm = norm * np.asarray([self.missing_[group_name][name] for name in self.names])
            norm = np.power(norm, 1.0 / len(self.groups))
            params = norm
        else:
            try:
                params = self.norm_.loc[groups].to_numpy()
            except (KeyError, TypeError):
                params = np.asarray([self.missing_[name] for name in self.names])
        return params

    def get_norm(self, X: pd.DataFrame) -> pd.DataFrame:
        """
        Get scaling parameters for multiple groups.

        Args:
            X (pd.DataFrame): dataframe with ``groups`` columns

        Returns:
            pd.DataFrame: dataframe with scaling parameterswhere each row corresponds to the input dataframe
        """
        if len(self.groups) == 0:
            norm = np.asarray(self.norm_).reshape(1, -1)
        elif self.scale_by_group:
            norm = [
                np.prod(
                    [
                        X[group_name]
                        .map(self.norm_[group_name][name])
                        .fillna(self.missing_[group_name][name])
                        .to_numpy()
                        for group_name in self.groups
                    ],
                    axis=0,
                )
                for name in self.names
            ]
            norm = np.power(np.stack(norm, axis=1), 1.0 / len(self.groups))
        else:
            norm = X[self.groups].set_index(self.groups).join(self.norm_).fillna(self.missing_).to_numpy()
        return norm
