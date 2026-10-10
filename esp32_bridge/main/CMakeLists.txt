idf_component_register(SRCS "main.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES usb nvs_flash esp_wifi esp_netif esp_event lwip)
