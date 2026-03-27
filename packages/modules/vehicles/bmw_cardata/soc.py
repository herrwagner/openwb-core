from modules.common.abstract_device import DeviceDescriptor
from modules.common.abstract_vehicle import VehicleUpdateData
from modules.common.component_state import CarState
from modules.common.configurable_vehicle import ConfigurableVehicle
from modules.vehicles.bmw_cardata import api
from modules.vehicles.bmw_cardata.config import BMWCarData


def create_vehicle(vehicle_config: BMWCarData, vehicle: int):
    def updater(vehicle_update_data: VehicleUpdateData) -> CarState:
        return api.Api().fetch_soc(
            vehicle_config.configuration.client_id,
            vehicle_config.configuration.refresh_token,
            vehicle_config.configuration.vin,
            vehicle_config.configuration.container_id,
            vehicle_config.configuration.token_expiry_buffer)
    return ConfigurableVehicle(vehicle_config=vehicle_config,
                               component_updater=updater,
                               vehicle=vehicle,
                               calc_while_charging=vehicle_config.configuration.calculate_soc)


device_descriptor = DeviceDescriptor(configuration_factory=BMWCarData)
