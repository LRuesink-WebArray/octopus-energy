import homey


class OctopusEnergyApp(homey.App):
    async def on_init(self) -> None:
        self.log("Octopus Energy app initialized")


homey_export = OctopusEnergyApp
