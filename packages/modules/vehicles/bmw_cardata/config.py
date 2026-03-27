from typing import Optional


class BMWCarDataConfiguration:
    def __init__(self,
                 client_id: Optional[str] = None,
                 refresh_token: Optional[str] = None,
                 vin: Optional[str] = None,
                 container_id: Optional[str] = None,
                 calculate_soc: bool = True,
                 token_expiry_buffer: int = 60):
        self.client_id = client_id
        self.refresh_token = refresh_token
        self.vin = vin
        self.container_id = container_id
        self.calculate_soc = calculate_soc
        self.token_expiry_buffer = token_expiry_buffer


class BMWCarData:
    def __init__(self,
                 name: str = "BMW CarData",
                 type: str = "bmw_cardata",
                 official: bool = False,
                 configuration: BMWCarDataConfiguration = None) -> None:
        self.name = name
        self.type = type
        self.official = official
        self.configuration = configuration or BMWCarDataConfiguration()
