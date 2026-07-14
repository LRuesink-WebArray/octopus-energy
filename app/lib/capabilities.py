"""Per-fuel capability definitions.

Single source of truth for which price capabilities belong to each fuel, so the
driver (pairing-time creation) and the device (runtime reconcile) stay in sync
with each other and with driver.compose.json's titles.
"""

ELECTRICITY_CAPS = [
    "measure_price_current.electricity",
    "measure_price_highest.electricity",
    "measure_price_lowest.electricity",
]

GAS_CAPS = [
    "measure_price_current.gas",
    "measure_price_highest.gas",
    "measure_price_lowest.gas",
]

# Titles mirror driver.compose.json so runtime-added capabilities read the same
# as ones created from the manifest.
CAPABILITY_OPTIONS = {
    "measure_price_current.electricity": {"title": {"en": "Current electricity price"}},
    "measure_price_highest.electricity": {"title": {"en": "Highest electricity price today"}},
    "measure_price_lowest.electricity": {"title": {"en": "Lowest electricity price today"}},
    "measure_price_current.gas": {"title": {"en": "Current gas price"}},
    "measure_price_highest.gas": {"title": {"en": "Highest gas price today"}},
    "measure_price_lowest.gas": {"title": {"en": "Lowest gas price today"}},
}
