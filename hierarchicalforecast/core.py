# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/core.ipynb.

# %% auto 0
__all__ = ['HierarchicalReconciliation']

# %% ../nbs/core.ipynb 3
import re
import gc
import time
import copy
from inspect import signature
from scipy.stats import norm
from scipy import sparse
from typing import Callable, Dict, List, Optional
import warnings

import numpy as np
import pandas as pd

# %% ../nbs/core.ipynb 5
def _build_fn_name(fn) -> str:
    fn_name = type(fn).__name__
    func_params = fn.__dict__

    # Take default parameter out of names
    args_to_remove = ['insample', 'num_threads']
    if not func_params.get('nonnegative', False):
        args_to_remove.append('nonnegative')

    if fn_name == 'MinTrace' and \
        func_params['method']=='mint_shrink':
        if func_params['mint_shr_ridge'] == 2e-8:
            args_to_remove += ['mint_shr_ridge']

    func_params = [f'{name}-{value}' for name, value in func_params.items() if name not in args_to_remove]
    if func_params:
        fn_name += '_' + '_'.join(func_params)
    return fn_name

# %% ../nbs/core.ipynb 9
def _reverse_engineer_sigmah(Y_hat_df, y_hat, model_name):
    """
    This function assumes that the model creates prediction intervals
    under a normality with the following the Equation:
    $\hat{y}_{t+h} + c \hat{sigma}_{h}$

    In the future, we might deprecate this function in favor of a 
    direct usage of an estimated $\hat{sigma}_{h}$
    """

    drop_cols = ['ds']
    if 'y' in Y_hat_df.columns:
        drop_cols.append('y')
    if model_name+'-median' in Y_hat_df.columns:
        drop_cols.append(model_name+'-median')
    model_names = Y_hat_df.drop(columns=drop_cols, axis=1).columns.to_list()
    pi_model_names = [name for name in model_names if ('-lo' in name or '-hi' in name)]
    pi_model_name = [pi_name for pi_name in pi_model_names if model_name in pi_name]
    pi = len(pi_model_name) > 0

    n_series = len(Y_hat_df.index.unique())

    if not pi:
        raise Exception(f'Please include `{model_name}` prediction intervals in `Y_hat_df`')

    pi_col = pi_model_name[0]
    sign = -1 if 'lo' in pi_col else 1
    level_col = re.findall('[\d]+[.,\d]+|[\d]*[.][\d]+|[\d]+', pi_col)
    level_col = float(level_col[-1])
    z = norm.ppf(0.5 + level_col / 200)
    sigmah = Y_hat_df[pi_col].values.reshape(n_series,-1)
    sigmah = sign * (sigmah - y_hat) / z

    return sigmah

# %% ../nbs/core.ipynb 10
class HierarchicalReconciliation:
    """Hierarchical Reconciliation Class.

    The `core.HierarchicalReconciliation` class allows you to efficiently fit multiple 
    HierarchicaForecast methods for a collection of time series and base predictions stored in 
    pandas DataFrames. The `Y_df` dataframe identifies series and datestamps with the unique_id and ds columns while the
    y column denotes the target time series variable. The `Y_h` dataframe stores the base predictions, 
    example ([AutoARIMA](https://nixtla.github.io/statsforecast/models.html#autoarima), [ETS](https://nixtla.github.io/statsforecast/models.html#autoets), etc.).

    **Parameters:**<br>
    `reconcilers`: A list of instantiated classes of the [reconciliation methods](https://nixtla.github.io/hierarchicalforecast/methods.html) module .<br>

    **References:**<br>
    [Rob J. Hyndman and George Athanasopoulos (2018). \"Forecasting principles and practice, Hierarchical and Grouped Series\".](https://otexts.com/fpp3/hierarchical.html)
    """
    def __init__(self,
                 reconcilers: List[Callable]):
        self.reconcilers = reconcilers
        self.orig_reconcilers = copy.deepcopy(reconcilers) # TODO: elegant solution
        self.insample = any([method.insample for method in reconcilers])
    
    def _prepare_fit(self,
                     Y_hat_df: pd.DataFrame,
                     S_df: pd.DataFrame,
                     Y_df: Optional[pd.DataFrame],
                     tags: Dict[str, np.ndarray],
                     level: Optional[List[int]] = None,
                     intervals_method: str = 'normality',
                     sort_df: bool = True):
        """
        Performs preliminary wrangling and protections
        """
        #-------------------------------- Match Y_hat/Y/S index order --------------------------------#
        if sort_df:
            Y_hat_df = Y_hat_df.reset_index()
            Y_hat_df.unique_id = Y_hat_df.unique_id.astype('category')
            Y_hat_df.unique_id = Y_hat_df.unique_id.cat.set_categories(S_df.index)
            Y_hat_df = Y_hat_df.sort_values(by=['unique_id', 'ds'])
            Y_hat_df = Y_hat_df.set_index('unique_id')

            if Y_df is not None:
                Y_df = Y_df.reset_index()
                Y_df.unique_id = Y_df.unique_id.astype('category')
                Y_df.unique_id = Y_df.unique_id.cat.set_categories(S_df.index)
                Y_df = Y_df.sort_values(by=['unique_id', 'ds'])
                Y_df = Y_df.set_index('unique_id')

            S_df.index = pd.CategoricalIndex(S_df.index, categories=S_df.index)

        #----------------------------------- Check Input's Validity ----------------------------------#
        # Check input's validity
        if intervals_method not in ['normality', 'bootstrap', 'permbu']:
            raise ValueError(f'Unkwon interval method: {intervals_method}')

        if self.insample or (intervals_method in ['bootstrap', 'permbu']):
            if Y_df is None:
                raise Exception('you need to pass `Y_df`')
        
        # Protect level list
        if (level is not None):
            level_outside_domain = np.any((np.array(level) < 0)|(np.array(level) >= 100 ))
            if level_outside_domain and (intervals_method in ['normality', 'permbu']):
                raise Exception('Level outside domain, send `level` list in [0,100)')

        # Declare output names
        drop_cols = ['ds', 'y'] if 'y' in Y_hat_df.columns else ['ds']
        model_names = Y_hat_df.drop(columns=drop_cols, axis=1).columns.to_list()

        # Ensure numeric columns
        if not len(Y_hat_df[model_names].select_dtypes(include='number').columns) == len(Y_hat_df[model_names].columns):
            raise Exception('`Y_hat_df`s columns contain non numeric types')
            
        #Ensure no null values
        if Y_hat_df[model_names].isnull().values.any():
            raise Exception('`Y_hat_df` contains null values')
        
        pi_model_names = [name for name in model_names if ('-lo' in name or '-hi' in name or '-median' in name)]
        model_names = [name for name in model_names if name not in pi_model_names]
        
        # TODO: Complete y_hat_insample protection
        if intervals_method in ['bootstrap', 'permbu']:
            if not (set(model_names) <= set(Y_df.columns)):
                raise Exception('Check `Y_hat_df`s models are included in `Y_df` columns')

        uids = Y_hat_df.index.unique()

        # Check Y_hat_df\S_df series difference
        S_diff  = len(S_df.index.difference(uids))
        Y_hat_diff = len(Y_hat_df.index.difference(S_df.index.unique()))
        if S_diff > 0 or Y_hat_diff > 0:
            raise Exception(f'Check `S_df`, `Y_hat_df` series difference, S\Y_hat={S_diff}, Y_hat\S={Y_hat_diff}')

        if Y_df is not None:
            # Check Y_hat_df\Y_df series difference
            Y_diff  = len(Y_df.index.difference(uids))
            Y_hat_diff = len(Y_hat_df.index.difference(Y_df.index.unique()))
            if Y_diff > 0 or Y_hat_diff > 0:
                raise Exception(f'Check `Y_hat_df`, `Y_df` series difference, Y_hat\Y={Y_hat_diff}, Y\Y_hat={Y_diff}')

        # Same Y_hat_df/S_df/Y_df's unique_id order to prevent errors
        S_df = S_df.loc[uids]

        return Y_hat_df, S_df, Y_df, model_names

    def reconcile(self, 
                  Y_hat_df: pd.DataFrame,
                  S: pd.DataFrame,
                  tags: Dict[str, np.ndarray],
                  Y_df: Optional[pd.DataFrame] = None,
                  level: Optional[List[int]] = None,
                  intervals_method: str = 'normality',
                  num_samples: int = -1,
                  seed: int = 0,
                  sort_df: bool = True,
                  is_balanced: bool = False,
        ):
        """Hierarchical Reconciliation Method.

        The `reconcile` method is analogous to SKLearn `fit_predict` method, it 
        applies different reconciliation techniques instantiated in the `reconcilers` list.

        Most reconciliation methods can be described by the following convenient 
        linear algebra notation:

        $$\\tilde{\mathbf{y}}_{[a,b],\\tau} = \mathbf{S}_{[a,b][b]} \mathbf{P}_{[b][a,b]} \hat{\mathbf{y}}_{[a,b],\\tau}$$

        where $a, b$ represent the aggregate and bottom levels, $\mathbf{S}_{[a,b][b]}$ contains
        the hierarchical aggregation constraints, and $\mathbf{P}_{[b][a,b]}$ varies across 
        reconciliation methods. The reconciled predictions are $\\tilde{\mathbf{y}}_{[a,b],\\tau}$, and the 
        base predictions $\hat{\mathbf{y}}_{[a,b],\\tau}$.

        **Parameters:**<br>
        `Y_hat_df`: pd.DataFrame, base forecasts with columns `ds` and models to reconcile indexed by `unique_id`.<br>
        `Y_df`: pd.DataFrame, training set of base time series with columns `['ds', 'y']` indexed by `unique_id`.<br>
        If a class of `self.reconciles` receives `y_hat_insample`, `Y_df` must include them as columns.<br>
        `S`: pd.DataFrame with summing matrix of size `(base, bottom)`, see [aggregate method](https://nixtla.github.io/hierarchicalforecast/utils.html#aggregate).<br>
        `tags`: Each key is a level and its value contains tags associated to that level.<br>
        `level`: positive float list [0,100), confidence levels for prediction intervals.<br>
        `intervals_method`: str, method used to calculate prediction intervals, one of `normality`, `bootstrap`, `permbu`.<br>
        `num_samples`: int=-1, if positive return that many probabilistic coherent samples.
        `seed`: int=0, random seed for numpy generator's replicability.<br>
        `sort_df` : bool (default=True), if True, sort `df` by [`unique_id`,`ds`].<br>
        `is_balanced`: bool=False, wether `Y_df` is balanced, set it to True to speed things up if `Y_df` is balanced.<br>

        **Returns:**<br>
        `Y_tilde_df`: pd.DataFrame, with reconciled predictions.
        """
        # Check input's validity and sort dataframes
        Y_hat_df, S_df, Y_df, self.model_names = \
                    self._prepare_fit(Y_hat_df=Y_hat_df,
                                      S_df=S,
                                      Y_df=Y_df,
                                      tags=tags,
                                      level=level,
                                      intervals_method=intervals_method,
                                      sort_df=sort_df)

        # Initialize reconciler arguments
        reconciler_args = dict(
            idx_bottom=S_df.index.get_indexer(S.columns),
            tags={key: S_df.index.get_indexer(val) for key, val in tags.items()}
        )

        any_sparse = any([method.is_sparse_method for method in self.reconcilers])
        if any_sparse:
            try:
                S_for_sparse = sparse.csr_matrix(S_df.sparse.to_coo())
            except AttributeError:
                warnings.warn('Using dense S matrix for sparse reconciliation method.')
                S_for_sparse = S_df.values.astype(np.float32)

        if Y_df is not None:
            if is_balanced:
                y_insample = Y_df['y'].values.reshape(len(S_df), -1).astype(np.float32)
            else:
                y_insample = Y_df.pivot(columns='ds', values='y').loc[S_df.index].values.astype(np.float32)
            reconciler_args['y_insample'] = y_insample

        Y_tilde_df= Y_hat_df.copy()
        start = time.time()
        self.execution_times = {}
        self.level_names = {}
        self.sample_names = {}
        for reconcile_fn, name_copy in zip(self.reconcilers, self.orig_reconcilers):
            reconcile_fn_name = _build_fn_name(name_copy)

            if reconcile_fn.is_sparse_method:
                reconciler_args["S"] = S_for_sparse
            else:
                reconciler_args["S"] = S_df.values.astype(np.float32)

            has_fitted = 'y_hat_insample' in signature(reconcile_fn).parameters
            has_level = 'level' in signature(reconcile_fn).parameters

            for model_name in self.model_names:
                recmodel_name = f'{model_name}/{reconcile_fn_name}'
                y_hat = Y_hat_df[model_name].values.reshape(len(S_df), -1).astype(np.float32)
                reconciler_args['y_hat'] = y_hat

                if (self.insample and has_fitted) or intervals_method in ['bootstrap', 'permbu']:
                    if is_balanced:
                        y_hat_insample = Y_df[model_name].values.reshape(len(S_df), -1).astype(np.float32)
                    else:
                        y_hat_insample = Y_df.pivot(columns='ds', values=model_name).loc[S_df.index].values.astype(np.float32)
                    reconciler_args['y_hat_insample'] = y_hat_insample

                if has_level and (level is not None):
                    if intervals_method in ['normality', 'permbu']:
                        sigmah = _reverse_engineer_sigmah(Y_hat_df=Y_hat_df,
                                    y_hat=y_hat, model_name=model_name)
                        reconciler_args['sigmah'] = sigmah

                    reconciler_args['intervals_method'] = intervals_method
                    reconciler_args['num_samples'] = 200 # TODO: solve duplicated num_samples
                    reconciler_args['seed'] = seed

                # Mean and Probabilistic reconciliation
                kwargs = [key for key in signature(reconcile_fn).parameters if key in reconciler_args.keys()]
                kwargs = {key: reconciler_args[key] for key in kwargs}
                
                if (level is not None) and (num_samples > 0):
                    # Store reconciler's memory to generate samples
                    reconciler = reconcile_fn.fit(**kwargs)
                    fcsts_model = reconciler.predict(S=reconciler_args['S'], 
                                                     y_hat=reconciler_args['y_hat'], level=level)
                else:
                    # Memory efficient reconciler's fit_predict
                    fcsts_model = reconcile_fn(**kwargs, level=level)

                # Parse final outputs
                Y_tilde_df[recmodel_name] = fcsts_model['mean'].flatten()
                if intervals_method in ['bootstrap', 'normality', 'permbu'] and level is not None:
                    level.sort()
                    lo_names = [f'{recmodel_name}-lo-{lv}' for lv in reversed(level)]
                    hi_names = [f'{recmodel_name}-hi-{lv}' for lv in level]
                    self.level_names[recmodel_name] = lo_names + hi_names
                    sorted_quantiles = np.reshape(fcsts_model['quantiles'], (len(Y_tilde_df),-1))
                    intervals_df = pd.DataFrame(sorted_quantiles, index=Y_tilde_df.index,
                                                columns=self.level_names[recmodel_name])
                    Y_tilde_df= pd.concat([Y_tilde_df, intervals_df], axis=1)

                    if num_samples > 0:
                        samples = reconciler.sample(num_samples=num_samples)
                        self.sample_names[recmodel_name] = [f'{recmodel_name}-sample-{i}' for i in range(num_samples)]
                        samples = np.reshape(samples, (len(Y_tilde_df),-1))
                        samples_df = pd.DataFrame(samples, index=Y_tilde_df.index,
                                                  columns=self.sample_names[recmodel_name])
                        Y_tilde_df= pd.concat([Y_tilde_df, samples_df], axis=1)

                    del sorted_quantiles
                    del intervals_df
                if self.insample and has_fitted:
                    del y_hat_insample
                gc.collect()

                end = time.time()
                self.execution_times[f'{model_name}/{reconcile_fn_name}'] = (end - start)

        return Y_tilde_df

    def bootstrap_reconcile(self,
                            Y_hat_df: pd.DataFrame,
                            S_df: pd.DataFrame,
                            tags: Dict[str, np.ndarray],
                            Y_df: Optional[pd.DataFrame] = None,
                            level: Optional[List[int]] = None,
                            intervals_method: str = 'normality',
                            num_samples: int = -1,
                            num_seeds: int = 1,
                            sort_df: bool = True):
        """Bootstraped Hierarchical Reconciliation Method.

        Applies N times, based on different random seeds, the `reconcile` method 
        for the different reconciliation techniques instantiated in the `reconcilers` list. 

        **Parameters:**<br>
        `Y_hat_df`: pd.DataFrame, base forecasts with columns `ds` and models to reconcile indexed by `unique_id`.<br>
        `Y_df`: pd.DataFrame, training set of base time series with columns `['ds', 'y']` indexed by `unique_id`.<br>
        If a class of `self.reconciles` receives `y_hat_insample`, `Y_df` must include them as columns.<br>
        `S`: pd.DataFrame with summing matrix of size `(base, bottom)`, see [aggregate method](https://nixtla.github.io/hierarchicalforecast/utils.html#aggregate).<br>
        `tags`: Each key is a level and its value contains tags associated to that level.<br>
        `level`: positive float list [0,100), confidence levels for prediction intervals.<br>
        `intervals_method`: str, method used to calculate prediction intervals, one of `normality`, `bootstrap`, `permbu`.<br>
        `num_samples`: int=-1, if positive return that many probabilistic coherent samples.
        `num_seeds`: int=1, random seed for numpy generator's replicability.<br>
        `sort_df` : bool (default=True), if True, sort `df` by [`unique_id`,`ds`].<br>

        **Returns:**<br>
        `Y_bootstrap_df`: pd.DataFrame, with bootstraped reconciled predictions.
        """

        # Check input's validity and sort dataframes
        Y_hat_df, S_df, Y_df, self.model_names = \
                    self._prepare_fit(Y_hat_df=Y_hat_df,
                                      S_df=S_df,
                                      Y_df=Y_df,
                                      tags=tags,
                                      intervals_method=intervals_method,
                                      sort_df=sort_df)

        # Bootstrap reconciled predictions
        Y_tilde_list = []
        for seed in range(num_seeds):
            Y_tilde_df = self.reconcile(Y_hat_df=Y_hat_df,
                                        S=S_df,
                                        tags=tags,
                                        Y_df=Y_df,
                                        level=level,
                                        intervals_method=intervals_method,
                                        num_samples=num_samples,
                                        seed=seed,
                                        sort_df=False)
            Y_tilde_df['seed'] = seed
            # TODO: fix broken recmodel_names
            if seed==0:
                first_columns = Y_tilde_df.columns
            Y_tilde_df.columns = first_columns
            Y_tilde_list.append(Y_tilde_df)

        Y_bootstrap_df = pd.concat(Y_tilde_list, axis=0)
        del Y_tilde_list
        gc.collect()

        return Y_bootstrap_df
