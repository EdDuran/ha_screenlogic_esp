# Pool ESP Integration

## Description
The Pool ESP (Estimate to Set Point) Integration answers the age-old question:

> **_When will the Spa be ready?_**

Pool ESP will automatically discovers your Pentair Screenlogic Device and creates it's own ESP Entities. Utilizing Home Assistant historically recorded data, Pool and Spa heating sessions are analyzed to calculate the heating rate and estimate when the Water Temperature Set Point will be reached.

The HA Recorder defaults to purge data after 10 days; Pool ESP can only look at data that far back in time. The Recorder purge can be adjusted in your [configuration.yaml](#configuration-steps):

To provide more useful estimates, Pool ESP also maintains heating rates in private storage for up to a year. The data here is also pruned and purged to alieviate unbounded growth.

Two sensor entities are created by Pool ESP; one for the Pool and one for the Spa. These entities provide the [State of Operation](#states) and additional **Attributes**:

| Attribute      | Description |
| -------------- | ----------- |
| body           | "pool" or "spa" |
| status         | Off, Sensing, Learning, D-HH:MM, Ready, Standby, Maintaining |
| seconds        | The number of **Sensing** seconds remaining, or number of seconds untill Ready |
| confidence_pct | Confidence Percentage, based on the accumulated heating data |
| confidence     | Confidence Label { low, medium, high } |

**NOTES:**
* D-HH:MM .. Days (omitted if 0), Hours and Minutes until the water will be ready.
* Once sufficient heating characteristics are determined, Pool ESP can determin the estimate when the heater is off, thus can provide letting you know:

> **_What if I turned on the Spa?_**

## Prerequisites
Pool ESP currently relies on data provided by the Pentair Screenlogic Integration. If you do not have a Pentair Screenlogic system and the Screenlogic Integration installed .. Pool ESP will not work. 

## Installation via HACS

Look for "Pool ESP" and install

## Configuration steps
Pool ESP is Zero Config. The Integration automatically identifies your Pentair Screenlogic Device
and creates it's own Sensor Entities.

For example, Pool ESP discovers Device "**Pentair: 11-22-33**" and creates two sensors:
* sensor.pentair_21_ce_68_pool_esp
* sensor.pentair_21_ce_68_spa_esp

If no suitable Device is found, the Integration setup displays an error message.

### HA Recorder
You can adjust how long the HA Recorder maintains historical data by adding to your **configuration.yaml**:

```
  recorder:
   purge_keep_days: 30  # Adjust to your desired number of days
   auto_purge: true
   auto_repack: true
```

## How ESP learning works
Pool ESP relies on the Home Assistant Recorder to gather data. If you've recently heated your Pool (or Spa), ESP can instantly produce estimates. If there is no useful data yet, ESP is "Learning". When sufficient heating data is available ESP will calculate how long it'll take to reach your desired Set Point. ESP provides a Confidence percentage (determined by the quality and quantity data available). The Confidence could start low (10%) and gradually increase over time as more quality data becomes available.

Pool ESP looks at how long it takes the Pool or Spa to increase in temperature and determines a rate. It does this in 5 degree Air Temperature bins, thus cooler air temperatures would result in longer rates than wamer air temperatures. This will be evident in the Rate Viewer.

It's interesting to note, the larger Pool volume is more affected by Air Temperature than a small Spa would be. This actually makes sense.

## Watchdog
Once Pool ESP understands the heating characteristics it can alert if the heating time exceeds expectations. This FYI notification could be a natural event, such a very cold day, or indicate there is a heater malfunction which needs investigation.

## States
Pool ESP sensor entities provide the State of Operation:

| Entity State  | Description | Status Attribute |
| ------------- | ----------- | ---------------- |
| off         | Water is not being heated | Learning or D-HH:MM |
| sensing     | Heating; 2 minute delay, waiting for the Water Temperature to stabilize | Sensing |
| heating     | Activly heating the Water | Learning or D-HH:MM |
| ready       | Water Temperature has reached the Set Point | Ready |
| standby     | At Set Point, heating paused | Ready |
| maintaining | At Set Point, heating the Water to maintain temperature | Ready |

**NOTE:**
* State **ready** will only occur once and can be used in an Automation to alert: "The Spa is ready"
* States **standby** and **maintaining** alternate as the Heater turns on and off to keep the water at temperature

## Sample Dashboard
In this example, the Spa Circuit is off, Heater enabled but not heating. Water is 98°, set to 99°. The ESP State is off (because the Circuit is off) and estimating that if it were turned on, it would take about 5 minutes to heat up.
<img width="300" height="500" alt="ESP Spa Dashboard" src="https://github.com/user-attachments/assets/a6e3a00e-d755-4eb4-adbc-68eb88f4b6b7" />

## ESP Rate Viewer
The Pool ESP settings (gear icon) has an option to add the Rate Viewer into the Home Assistant Dashboard Sidebar. Pool ESP algorithmically prunes "bad data" over time and the Viewer allows you to proactivly delete extraneous values.

<img width="1886" height="1018" alt="ESP Rate Viewer" src="https://github.com/user-attachments/assets/4ebb96b9-1750-41b8-833b-a70abe0a7e94" />

### Extraneous Values?
If your heater malfunctions (ie, heat pump fails to start a few times before functioning) _Screenlogic thinks it heating_ but the heat pump hasn't started properly, the rate estimate can be exaggerated. These bogus values will age out over time, or can be manually removed. The Viewer will also flag duplicate data. This can be a normal behavior and simply an FYI

Click on the Air Temperature bin to open the list of data rates. Click the checkbox on the rates you wish to delete, [Delete Selected] button, then [Save to HA]

<img width="1912" height="1344" alt="ESP Rate Viewer Delete" src="https://github.com/user-attachments/assets/fa0dd683-66bf-4d11-b613-5f6b7bedd237" />

