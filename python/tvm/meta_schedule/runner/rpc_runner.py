# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""RPC Runner"""
import concurrent.futures
from contextlib import contextmanager
import itertools
import os.path as osp
from typing import Any, Callable, Dict, List, Optional, Union

from tvm.contrib.popen_pool import PopenPoolExecutor
from tvm.rpc import RPCSession
from tvm.runtime import Device, Module, ndarray

from ..utils import (
    get_global_func_on_rpc_session,
    get_global_func_with_default_on_worker,
)
from .config import EvaluatorConfig, RPCConfig
from .runner import PyRunner, RunnerFuture, RunnerInput, RunnerResult


class RPCRunnerFuture(RunnerFuture):
    """RPC based runner future

    Parameters
    ----------
    future: concurrent.futures.Future
        The concurrent function to check when the function is done and to return the result.
    timeout_sec: float
        The timeout in seconds.
    """

    future: concurrent.futures.Future
    timeout_sec: float

    def __init__(self, future: concurrent.futures.Future, timeout_sec: float) -> None:
        """Constructor

        Parameters
        ----------
        future: concurrent.futures.Future
            The concurrent function to check when the function is done and to return the result.
        timeout_sec: float
            The timeout in seconds.
        """
        super().__init__()
        self.future = future
        self.timeout_sec = timeout_sec

    def done(self) -> bool:
        return self.future.done()

    def result(self) -> RunnerResult:
        try:
            run_secs: List[float] = self.future.result()
        except TimeoutError as exception:
            return RunnerResult(
                None,
                error_msg=f"RPCRunner: Timeout, killed after {self.timeout_sec} seconds",
            )
        except Exception as exception:  # pylint: disable=broad-except
            return RunnerResult(
                None,
                error_msg="RPCRunner: An exception occurred\n" + str(exception),
            )
        return RunnerResult(run_secs, None)


T_ARG_INFO_JSON_OBJ = List[Any]  # pylint: disable=invalid-name
T_ARG_INFO_JSON_OBJ_LIST = List[T_ARG_INFO_JSON_OBJ]  # pylint: disable=invalid-name
T_ARGUMENT = Any  # pylint: disable=invalid-name
T_ARGUMENT_LIST = List[T_ARGUMENT]  # pylint: disable=invalid-name


class RPCRunner(PyRunner):
    """RPC based runner

    Parameters
    ----------
    rpc_config: RPCConfig
        The rpc configuration.
    evaluator_config: EvaluatorConfig
        The evaluator configuration.
    cooldown_sec: float
        The cooldown in seconds. TODO(@junrushao1994,@zxybazh): This is not used yet.
    alloc_repeat: int
        The number of times to repeat the allocation.
    f_create_session: Optional[str, Callable]
        The function name to create the session or the function itself.
    f_upload_module: Optional[str, Callable]
        The function name to upload the module or the function itself.
    f_alloc_argument: Optional[str, Callable]
        The function name to allocate the arguments or the function itself.
    f_run_evaluator: Optional[str, Callable]
        The function name to run the evaluator or the function itself.
    f_cleanup: Optional[str, Callable]
        The function name to cleanup the session or the function itself.
    pool: PopenPoolExecutor
        The popen pool executor.

    Attributes
    ----------
    T_CREATE_SESSION : typing._GenericAlias
        The signature of the function `f_create_session`, which is:

        .. code-block:: python

        def default_create_session(rpc_config: RPCConfig) -> RPCSession:
            ...

    T_UPLOAD_MODULE : typing._GenericAlias
        The signature of the function `f_upload_module`, which is:

        .. code-block:: python

        def default_upload_module(
            session: RPCSession,
            local_path: str,
            remote_path: str,
        ) -> Module:
            ...

    T_ALLOC_ARGUMENT : typing._GenericAlias
        The signature of the function `f_alloc_argument`, which is:

        .. code-block:: python

        def default_alloc_argument(
            session: RPCSession,
            device: Device,
            args_info: T_ARG_INFO_JSON_OBJ_LIST,
            alloc_repeat: int,
        ) -> List[T_ARGUMENT_LIST]:
            ...

    T_RUN_EVALUATOR : typing._GenericAlias
        The signature of the function `f_run_evaluator`, which is:

        .. code-block:: python

        def default_run_evaluator(
            session: RPCSession,
            rt_mod: Module,
            device: Device,
            evaluator_config: EvaluatorConfig,
            repeated_args: List[T_ARGUMENT_LIST],
        ) -> List[float]:
            ...

    T_CLEANUP : typing._GenericAlias
        The signature of the function `f_cleanup`, which is:

        .. code-block:: python

        def default_cleanup(
            session: Optional[RPCSession],
            remote_path: Optional[str],
        ) -> None:
            ...
    """

    T_CREATE_SESSION = Callable[
        [RPCConfig],  # The RPC configuration
        RPCSession,  # The RPC Session
    ]
    T_UPLOAD_MODULE = Callable[
        [
            RPCSession,  # The RPC Session
            str,  # local path to the artifact
            str,  # remote path to the artifact
        ],
        Module,  # the Module opened on the remote
    ]
    T_ALLOC_ARGUMENT = Callable[
        [
            RPCSession,  # The RPC Session
            Device,  # The device on the remote
            T_ARG_INFO_JSON_OBJ_LIST,  # The metadata information of the arguments to be allocated
            int,  # The number of repeated allocations to be done
        ],
        List[T_ARGUMENT_LIST],  # A list of argument lists
    ]
    T_RUN_EVALUATOR = Callable[
        [
            RPCSession,  # The RPC Session
            Module,  # The Module opened on the remote
            Device,  # The device on the remote
            EvaluatorConfig,  # The evaluator configuration
            List[T_ARGUMENT_LIST],  # A list of argument lists
        ],
        List[float],  # A list of running time
    ]
    T_CLEANUP = Callable[
        [
            Optional[RPCSession],  # The RPC Session to be cleaned up
            Optional[str],  # remote path to the artifact
        ],
        None,
    ]

    rpc_config: RPCConfig
    evaluator_config: EvaluatorConfig
    cooldown_sec: float
    alloc_repeat: int

    f_create_session: Union[T_CREATE_SESSION, str, None]
    f_upload_module: Union[T_UPLOAD_MODULE, str, None]
    f_alloc_argument: Union[T_ALLOC_ARGUMENT, str, None]
    f_run_evaluator: Union[T_RUN_EVALUATOR, str, None]
    f_cleanup: Union[T_CLEANUP, str, None]

    pool: PopenPoolExecutor

    def __init__(
        self,
        rpc_config: Optional[RPCConfig] = None,
        evaluator_config: Optional[EvaluatorConfig] = None,
        cooldown_sec: float = 0.0,
        alloc_repeat: int = 1,
        f_create_session: Union[T_CREATE_SESSION, str, None] = None,
        f_upload_module: Union[T_UPLOAD_MODULE, str, None] = None,
        f_alloc_argument: Union[T_ALLOC_ARGUMENT, str, None] = None,
        f_run_evaluator: Union[T_RUN_EVALUATOR, str, None] = None,
        f_cleanup: Union[T_CLEANUP, str, None] = None,
        max_workers: int = 1,
        initializer: Optional[Callable[[], None]] = None,
    ) -> None:
        """Constructor

        Parameters
        ----------
        rpc_config: RPCConfig
            The rpc configuration.
        evaluator_config: EvaluatorConfig
            The evaluator configuration.
        cooldown_sec: float
            The cooldown in seconds.
        alloc_repeat: int
            The number of times to random fill the allocation.
        f_create_session: Union[T_CREATE_SESSION, str, None]
            The function name to create the session or the function itself.
        f_upload_module: Union[T_UPLOAD_MODULE, str, None]
            The function name to upload the module or the function itself.
        f_alloc_argument: Union[T_ALLOC_ARGUMENT, str, None]
            The function name to allocate the arguments or the function itself.
        f_run_evaluator: Union[T_RUN_EVALUATOR, str, None]
            The function name to run the evaluator or the function itself.
        f_cleanup: Union[T_CLEANUP, str, None]
            The function name to cleanup the session or the function itself.
        max_workers: int = 1
            The maximum number of connections. Defaults to 1.
        initializer: Optional[Callable[[], None]]
            The initializer function.
        """
        super().__init__()
        self.rpc_config = RPCConfig._normalized(rpc_config)
        self.evaluator_config = EvaluatorConfig._normalized(evaluator_config)
        self.cooldown_sec = cooldown_sec
        self.alloc_repeat = alloc_repeat
        self.f_create_session = f_create_session
        self.f_upload_module = f_upload_module
        self.f_alloc_argument = f_alloc_argument
        self.f_run_evaluator = f_run_evaluator
        self.f_cleanup = f_cleanup
        self.pool = PopenPoolExecutor(
            max_workers=max_workers,
            timeout=rpc_config.session_timeout_sec,
            initializer=initializer,
        )
        self._sanity_check()

    def run(self, runner_inputs: List[RunnerInput]) -> List[RunnerFuture]:
        results: List[RunnerFuture] = []
        for runner_input in runner_inputs:
            future = RPCRunnerFuture(
                future=self.pool.submit(
                    RPCRunner._worker_func,
                    self.f_create_session,
                    self.f_upload_module,
                    self.f_alloc_argument,
                    self.f_run_evaluator,
                    self.f_cleanup,
                    self.rpc_config,
                    self.evaluator_config,
                    self.alloc_repeat,
                    str(runner_input.artifact_path),
                    str(runner_input.device_type),
                    tuple(arg_info.as_json() for arg_info in runner_input.args_info),
                ),
                timeout_sec=self.rpc_config.session_timeout_sec,
            )
            results.append(future)
        return results

    def _sanity_check(self) -> None:
        def _check(
            f_create_session,
            f_upload_module,
            f_alloc_argument,
            f_run_evaluator,
            f_cleanup,
        ) -> None:
            get_global_func_with_default_on_worker(name=f_create_session, default=None)
            get_global_func_with_default_on_worker(name=f_upload_module, default=None)
            get_global_func_with_default_on_worker(name=f_alloc_argument, default=None)
            get_global_func_with_default_on_worker(name=f_run_evaluator, default=None)
            get_global_func_with_default_on_worker(name=f_cleanup, default=None)

        value = self.pool.submit(
            _check,
            self.f_create_session,
            self.f_upload_module,
            self.f_alloc_argument,
            self.f_run_evaluator,
            self.f_cleanup,
        )
        value.result()

    @staticmethod
    def _worker_func(
        _f_create_session: Union[T_CREATE_SESSION, str, None],
        _f_upload_module: Union[T_UPLOAD_MODULE, str, None],
        _f_alloc_argument: Union[T_ALLOC_ARGUMENT, str, None],
        _f_run_evaluator: Union[T_RUN_EVALUATOR, str, None],
        _f_cleanup: Union[T_CLEANUP, str, None],
        rpc_config: RPCConfig,
        evaluator_config: EvaluatorConfig,
        alloc_repeat: int,
        artifact_path: str,
        device_type: str,
        args_info: T_ARG_INFO_JSON_OBJ_LIST,
    ) -> List[float]:
        # Step 0. Get the registered functions
        f_create_session: RPCRunner.T_CREATE_SESSION = get_global_func_with_default_on_worker(
            _f_create_session, default_create_session
        )
        f_upload_module: RPCRunner.T_UPLOAD_MODULE = get_global_func_with_default_on_worker(
            _f_upload_module, default_upload_module
        )
        f_alloc_argument: RPCRunner.T_ALLOC_ARGUMENT = get_global_func_with_default_on_worker(
            _f_alloc_argument, default_alloc_argument
        )
        f_run_evaluator: RPCRunner.T_RUN_EVALUATOR = get_global_func_with_default_on_worker(
            _f_run_evaluator, default_run_evaluator
        )
        f_cleanup: RPCRunner.T_CLEANUP = get_global_func_with_default_on_worker(
            _f_cleanup, default_cleanup
        )
        # Managed resources
        session: Optional[RPCSession] = None
        remote_path: Optional[str] = None

        @contextmanager
        def resource_handler():
            try:
                yield
            finally:
                # Step 5. Clean up
                f_cleanup(session, remote_path)

        with resource_handler():
            # Step 1. Create session
            session = f_create_session(rpc_config)
            device = session.device(dev_type=device_type, dev_id=0)
            # Step 2. Upload the module
            _, remote_path = osp.split(artifact_path)
            local_path: str = artifact_path
            rt_mod: Module = f_upload_module(session, local_path, remote_path)
            # Step 3: Allocate input arguments
            repeated_args: List[T_ARGUMENT_LIST] = f_alloc_argument(
                session,
                device,
                args_info,
                alloc_repeat,
            )
            # Step 4: Run time_evaluator
            costs: List[float] = f_run_evaluator(
                session,
                rt_mod,
                device,
                evaluator_config,
                repeated_args,
            )
        return costs


def default_create_session(rpc_config: RPCConfig) -> RPCSession:
    """Default function to create the session

    Parameters
    ----------
    rpc_config : RPCConfig
        The configuration of the RPC session

    Returns
    -------
    session : RPCSession
        The created rpc session
    """
    return rpc_config.connect_server()


def default_upload_module(
    session: RPCSession,
    local_path: str,
    remote_path: str,
) -> Module:
    """Default function to upload the module

    Parameters
    ----------
    session: RPCSession
        The session to upload the module
    local_path: str
        The local path of the module
    remote_path: str
        The remote path to place the module

    Returns
    -------
    rt_mod : Module
        The runtime module
    """
    session.upload(local_path, remote_path)
    rt_mod: Module = session.load_module(remote_path)
    return rt_mod


def default_alloc_argument(
    session: RPCSession,
    device: Device,
    args_info: T_ARG_INFO_JSON_OBJ_LIST,
    alloc_repeat: int,
) -> List[T_ARGUMENT_LIST]:
    """Default function to allocate the arguments

    Parameters
    ----------
    session: RPCSession
        The session to allocate the arguments
    device: Device
        The device to allocate the arguments
    alloc_repeat: int
        The number of times to repeat the allocation
    args_info: PyArgsInfo
        The arguments info

    Returns
    -------
    repeated_args: List[Args]
        The allocation args
    """
    f_random_fill = get_global_func_on_rpc_session(
        session,
        "tvm.contrib.random.random_fill",
        "Please make sure 'USE_RANDOM' is turned ON in the config.cmake on the RPC server.",
    )

    def alloc_tensor(_, dtype, shape) -> ndarray.NDArray:
        arg = ndarray.empty(shape=shape, dtype=dtype, device=device)
        f_random_fill(arg)
        return arg

    def alloc_fail(*arg_info) -> None:
        raise NotImplementedError(arg_info)

    dispatcher: Dict[Any, Callable] = {
        "TENSOR": alloc_tensor,
        None: alloc_fail,
    }

    repeated_args: List[T_ARGUMENT_LIST] = []
    for _ in range(alloc_repeat):
        args: T_ARGUMENT_LIST = []
        arg_info: T_ARG_INFO_JSON_OBJ
        for arg_info in args_info:
            arg_type = arg_info[0]
            arg: Any = dispatcher.get(arg_type, None)(*arg_info)
            args.append(arg)
        repeated_args.append(args)
    return repeated_args


def default_run_evaluator(
    session: RPCSession,  # pylint: disable=unused-argument
    rt_mod: Module,
    device: Device,
    evaluator_config: EvaluatorConfig,
    repeated_args: List[T_ARGUMENT_LIST],
) -> List[float]:
    """Default function to run the evaluator

    Parameters
    ----------
    session: RPCSession
        The session to run the evaluator
    rt_mod: Module
        The runtime module
    device: Device
        The device to run the evaluator
    evaluator_config: EvaluatorConfig
        The evaluator config
    repeated_args: List[Args]
        The repeated arguments

    Returns
    -------
    costs: List[float]
        The evaluator results
    """
    evaluator = rt_mod.time_evaluator(
        func_name=rt_mod.entry_name,
        dev=device,
        number=evaluator_config.number,
        repeat=evaluator_config.repeat,
        min_repeat_ms=evaluator_config.min_repeat_ms,
        f_preproc="cache_flush_cpu_non_first_arg"
        if evaluator_config.enable_cpu_cache_flush
        else "",
    )
    repeated_costs: List[List[float]] = []
    for args in repeated_args:
        device.sync()
        profile_result = evaluator(*args)
        repeated_costs.append(profile_result.results)
    costs = [float(cost) for cost in itertools.chain.from_iterable(repeated_costs)]
    return costs


def default_cleanup(
    session: Optional[RPCSession],
    remote_path: Optional[str],
) -> None:
    """Default function to clean up the session

    Parameters
    ----------
    session: RPCSession
        The session to clean up
    remote_path: str
        The remote path to clean up
    """
    if session is not None and remote_path is not None:
        session.remove(remote_path)
        session.remove(remote_path + ".so")
        session.remove("")
