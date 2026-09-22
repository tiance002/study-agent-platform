from app.deployment import DeploymentSettings
from app.teaching.openai_provider import OpenAIResponsesProvider
from app.teaching.providers_factory import build_teaching_provider


def test_unlimited_platform_cap_is_explicit_and_validated():
    settings = DeploymentSettings.load(
        {"STUDY_PLATFORM_MONTHLY_CAP_MICRO": "unlimited"}
    )

    assert settings.platform_monthly_cap_micro is None
    assert "平台月度额度不能为负" not in settings.configuration_problems()


def test_provider_factory_uses_the_explicit_settings_mapping(monkeypatch):
    monkeypatch.delenv("STUDY_PLATFORM_TEACHING_API_KEY", raising=False)
    monkeypatch.delenv("STUDY_PLATFORM_TEACHING_BASE_URL", raising=False)
    settings = DeploymentSettings.load(
        {
            "STUDY_PLATFORM_TEACHING_PROVIDER": "openai",
            "STUDY_PLATFORM_TEACHING_MODEL": "deepseek-flash",
            "STUDY_PLATFORM_TEACHING_API_KEY": "mapping-secret",
            "STUDY_PLATFORM_TEACHING_BASE_URL": "https://provider.example/v1/",
        }
    )

    provider = build_teaching_provider(settings)

    assert isinstance(provider, OpenAIResponsesProvider)
    assert provider._api_key == "mapping-secret"
    assert provider._base_url == "https://provider.example/v1"
    assert "mapping-secret" not in repr(settings)
