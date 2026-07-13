from homey.app import App


class OctopusEnergyApp(App):
    async def on_init(self) -> None:
        self.log("Octopus Energy app initialized")


homey_export = OctopusEnergyApp