"""Tests for ``Stage`` enum and the ``__init_subclass__`` STAGE enforcement."""

from __future__ import annotations

import pytest

from dexpace.sdk.core.pipeline.async_policy import AsyncPolicy
from dexpace.sdk.core.pipeline.policy import Policy
from dexpace.sdk.core.pipeline.stage import Stage


class TestStage:
    @pytest.mark.parametrize(
        "stage",
        [Stage.REDIRECT, Stage.RETRY, Stage.AUTH, Stage.LOGGING, Stage.SERDE, Stage.SEND],
    )
    def test_pillar_stages(self, stage: Stage) -> None:
        assert stage.is_pillar

    @pytest.mark.parametrize(
        "stage",
        [
            Stage.POST_REDIRECT,
            Stage.POST_RETRY,
            Stage.PRE_AUTH,
            Stage.POST_AUTH,
            Stage.PRE_LOGGING,
            Stage.POST_LOGGING,
            Stage.PRE_SERDE,
            Stage.POST_SERDE,
            Stage.PRE_SEND,
        ],
    )
    def test_non_pillar_stages(self, stage: Stage) -> None:
        assert not stage.is_pillar

    def test_stage_order(self) -> None:
        assert Stage.REDIRECT < Stage.RETRY < Stage.AUTH < Stage.LOGGING < Stage.SEND


class TestSyncPolicyEnforcement:
    def test_concrete_policy_without_STAGE_raises(self) -> None:  # noqa: N802
        with pytest.raises(TypeError, match="must declare STAGE"):

            class BadPolicy(Policy):
                def send(self, request: object, ctx: object) -> object:  # type: ignore[override]
                    return None

    def test_concrete_policy_with_STAGE_succeeds(self) -> None:  # noqa: N802
        class GoodPolicy(Policy):
            STAGE = Stage.POST_AUTH

            def send(self, request: object, ctx: object) -> object:  # type: ignore[override]
                return None

        assert GoodPolicy.STAGE is Stage.POST_AUTH

    def test_abstract_intermediate_policy_can_skip_STAGE(self) -> None:  # noqa: N802
        from abc import abstractmethod

        class IntermediatePolicy(Policy):
            @abstractmethod
            def custom_hook(self) -> None: ...

        assert (
            not hasattr(IntermediatePolicy, "STAGE") or "STAGE" not in IntermediatePolicy.__dict__
        )

    def test_inherited_STAGE_satisfies_check(self) -> None:  # noqa: N802
        class ParentPolicy(Policy):
            STAGE = Stage.RETRY

            def send(self, request: object, ctx: object) -> object:  # type: ignore[override]
                return None

        class ChildPolicy(ParentPolicy):
            pass

        assert ChildPolicy.STAGE is Stage.RETRY


class TestAsyncPolicyEnforcement:
    def test_concrete_async_policy_without_STAGE_raises(self) -> None:  # noqa: N802
        with pytest.raises(TypeError, match="must declare STAGE"):

            class BadAsyncPolicy(AsyncPolicy):
                async def send(self, request: object, ctx: object) -> object:  # type: ignore[override]
                    return None

    def test_concrete_async_policy_with_STAGE_succeeds(self) -> None:  # noqa: N802
        class GoodAsyncPolicy(AsyncPolicy):
            STAGE = Stage.AUTH

            async def send(self, request: object, ctx: object) -> object:  # type: ignore[override]
                return None

        assert GoodAsyncPolicy.STAGE is Stage.AUTH


class TestExistingPoliciesDeclareSTAGE:
    """Regression: every shipped concrete policy must declare STAGE."""

    def test_retry_policy(self) -> None:
        from dexpace.sdk.core.pipeline.policies.retry import RetryPolicy

        assert RetryPolicy.STAGE is Stage.RETRY

    def test_async_retry_policy(self) -> None:
        from dexpace.sdk.core.pipeline.policies.async_retry import AsyncRetryPolicy

        assert AsyncRetryPolicy.STAGE is Stage.RETRY

    def test_logging_policy(self) -> None:
        from dexpace.sdk.core.pipeline.policies.logging_policy import LoggingPolicy

        assert LoggingPolicy.STAGE is Stage.LOGGING

    def test_tracing_policy(self) -> None:
        from dexpace.sdk.core.pipeline.policies.tracing_policy import TracingPolicy

        assert TracingPolicy.STAGE is Stage.POST_LOGGING

    def test_bearer_token_policy(self) -> None:
        from dexpace.sdk.core.http.auth.policies import BearerTokenPolicy

        assert BearerTokenPolicy.STAGE is Stage.AUTH

    def test_async_bearer_token_policy(self) -> None:
        from dexpace.sdk.core.http.auth.policies import AsyncBearerTokenPolicy

        assert AsyncBearerTokenPolicy.STAGE is Stage.AUTH

    def test_basic_auth_policy(self) -> None:
        from dexpace.sdk.core.http.auth.policies import BasicAuthPolicy

        assert BasicAuthPolicy.STAGE is Stage.AUTH

    def test_key_credential_policy(self) -> None:
        from dexpace.sdk.core.http.auth.policies import KeyCredentialPolicy

        assert KeyCredentialPolicy.STAGE is Stage.AUTH
