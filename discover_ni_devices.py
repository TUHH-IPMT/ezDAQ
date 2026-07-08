import nidaqmx.system

system = nidaqmx.system.System.local()
for device in system.devices:
    print(device.name, "->", device.product_type)
    for chan in device.ai_physical_chans:
        print("   ", chan.name)