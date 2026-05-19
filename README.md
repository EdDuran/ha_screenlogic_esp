# What it does
The Pool ESP (Estimate to Set Point) Integration answers the age-old question: **When will the Spa be ready?**

Pool ESP will is automatically discover your Pentair Screenlogic Device and create it's own ESP Entities. Using the Home Assistant historically recorded data, Pool and Spa heating characteristics are analyzed to calculate an estimate of when the water will be ready.

The Recorder is typically configured to age-out data after 14 or 30 days. This means Pool ESP can only look back that far for data. To provide more usefule estimates, heating rates are also maintained in a private storage for up to a year. The data here is also pruned and ages-out to alieviate unbounded growth.

Two sensor entities are created by Pool ESP; one for the Pool and one for the Spa. These entities provide the [State of Operation](#states) and additional Attributes:

* body - "pool" or "spa"
* status  - The status of operation: Off, Sensing, Learning, D-HH:MM, Ready, Standby, Maintaining
* seconds - The number of Sensing seconds remaining, or number of seconds till Ready
* confidence_pct - The Confidence percentage based on the accumulation of heating data
* confidence - The Confidence label { low, medium, high } based on the percentage

D-HH:MM .. Days, Hours and Minutes until the water will be ready

# Prerequisites
Pool ESP currently relies on data provided by the Pentair Screenlogic Integration.

# Installation instructions

# Configuration steps
Pool ESP is Zero Config. The Integration automatically identifies your Pentair Screenlogic Device
and create Sensors of it's own.

For example, Pool ESP discovers Device "**Pentair: 11-22-33**" and creates two sensors:
* sensor.pentair_21_ce_68_pool_esp
* sensor.pentair_21_ce_68_spa_esp

If no suitable Device is found, the Integration setup displays an error message.

# How ESP learning works
Pool ESP relies on the Home Assistant Recorder to gather data. If you've recently heated your Pool (or Spa), ESP can instantly produce estimates. If there is no useful data yet, ESP is "Learning". When sufficient heating data is available ESP will calculate how long it'll take to reach your desired Set Point. ESP also provides a confidence score based on the amount of data available, which typically starts low (12%) and increases as more data becomes available.

## Watchdog
Once Pool ESP understands the heating characteristics it will alert you if heating exceeds those expectations. This could be a natural event, such a very cold day, or indicate there is a heater malfunction.

## States
Pool ESP sensor entity provides the State of Operation for each Body (Pool, Spa)
* off - Currently not heating
* sensing - Sensing the water temperature (takes about 2 minutes)
* heating - The Heater is activly heating the water.
* ready - The water temperature has reached the Set Point
* standby - The Heater has paused heating the water
* maintaining - The Heater is maintaining the water temperature

NOTE: State **ready** only occurs once and is a great mechanism to use in an Automation to alert you: "The Spa is ready sir". The Status Attribute will be "Ready"

States **standby** and **maintaining** alternate as the Heater turns on and off to keep the water at temperature. During this time the Status Attribute will continue to be "Ready"

## ESP Rate Viewer
The Pool ESP has an option to include the Rate Viewer in the Home Assistant Dashboard Sidebar. Pool ESP algorithmically prunes "bad data" over time and the Viewer allows you to proactivly delete extraneous values.

### Extraneous Values?
If your heater malfunctions (ie, heat pump fails to start a few times before functioning) _Screenlogic thinks it heating_ but the heat pump hasn't started properly, the rate estimate can be exaggerated. These bogus values will age out over time, or can be manually removed.

# Screenshot of the dashboard
