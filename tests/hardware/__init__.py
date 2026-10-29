"""Opt-in registration of the checkout's physical acceptance scenarios."""

from autospan.api import PluginDescriptor, PluginRegistrar, ScenarioContribution


class _Plugin:
    descriptor = PluginDescriptor(name="ble-acceptance", version="0.1.0", api_version=1)

    def register(self, registrar: PluginRegistrar) -> None:
        registrar.scenarios(
            ScenarioContribution(package="autospan_ble_acceptance", relative_path=".")
        )


PLUGIN = _Plugin()
