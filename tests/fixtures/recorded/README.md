# Recorded controller dumps

Dumps from real controllers go here. Every `*.json` in this folder is picked up by
`tests/test_fixtures.py` automatically (scrape end-to-end + plausibility checks).

Record one (read-only, FC03 only) from a machine that can reach the controller:

```sh
datakom-exporter dump --host <controller-ip> --name "D-509 MK3, engine stopped" \
    --out tests/fixtures/recorded/d509mk3_stopped.json
```

Then fill the `"expected"` section with values read off the controller display, e.g.
`"genset_battery_volts": 13.4`, `"genset_mode": 1`. Keys are metric samples as in the
`/metrics` output, labels sorted: `genset_voltage_ln_volts{phase="L1",source="genset"}`.

Useful states to record: stopped (STOP), running off load (TEST), mains failure / on load.
If `word_order_guess` in the dump differs from `word_order`, re-run with `--word-order`.
