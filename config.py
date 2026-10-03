"""
Shared configuration for Apple Health MCP server and preprocessor.
"""

# HK type identifiers → short names used as Parquet filenames and tool metric keys
HK_TYPE_MAP = {
    "HKQuantityTypeIdentifierStepCount":                      "steps",
    # HKQuantityTypeIdentifierHeartRate ("heart_rate") intentionally omitted:
    # 1.69M rows, by far the largest metric (~5x the next biggest combined).
    # Loading it risked exceeding the Fly.io VM's memory limit. Disabled at
    # the query layer (see get_heart_rate() in server.py) and here, so the
    # XML import no longer touches it either. heart_rate.parquet itself is
    # left untouched on disk — re-add this line to bring it back.
    "HKQuantityTypeIdentifierRestingHeartRate":                "resting_hr",
    "HKQuantityTypeIdentifierActiveEnergyBurned":             "active_energy",
    "HKQuantityTypeIdentifierBasalEnergyBurned":              "basal_energy",
    "HKQuantityTypeIdentifierDistanceWalkingRunning":         "distance_walk",
    "HKQuantityTypeIdentifierDistanceCycling":                "distance_cycling",
    "HKQuantityTypeIdentifierFlightsClimbed":                 "flights_climbed",
    "HKCategoryTypeIdentifierSleepAnalysis":                  "sleep",
    "HKQuantityTypeIdentifierBodyMass":                       "body_mass",
    "HKQuantityTypeIdentifierBodyMassIndex":                  "bmi",
    "HKQuantityTypeIdentifierBodyFatPercentage":              "body_fat",
    "HKQuantityTypeIdentifierLeanBodyMass":                   "lean_body_mass",
    "HKQuantityTypeIdentifierWalkingSpeed":                   "walking_speed",
    "HKQuantityTypeIdentifierAppleWalkingSteadiness":         "walking_steadiness",
    "HKQuantityTypeIdentifierDietaryEnergyConsumed":          "dietary_energy",
    "HKQuantityTypeIdentifierDietaryProtein":                 "dietary_protein",
    "HKQuantityTypeIdentifierDietaryCarbohydrates":           "dietary_carbs",
    "HKQuantityTypeIdentifierDietaryFatTotal":                "dietary_fat",
    "HKQuantityTypeIdentifierHeadphoneAudioExposure":         "headphone_audio",
    "HKQuantityTypeIdentifierWalkingStepLength":              "walking_step_length",
    "HKQuantityTypeIdentifierWalkingDoubleSupportPercentage": "walking_double_support",
    "HKQuantityTypeIdentifierWalkingAsymmetryPercentage":     "walking_asymmetry",
}

# Short names used by the MCP server to load Parquet files
SHORT_NAMES = sorted(set(HK_TYPE_MAP.values()))

# HK types whose value is a category string (not numeric)
CATEGORY_TYPES = {
    "HKCategoryTypeIdentifierSleepAnalysis",
}

SLEEP_VALUES = {
    "HKCategoryValueSleepAnalysisInBed": "InBed",
    "HKCategoryValueSleepAnalysisAsleepUnspecified": "Asleep",
    "HKCategoryValueSleepAnalysisAsleepCore": "Core",
    "HKCategoryValueSleepAnalysisAsleepDeep": "Deep",
    "HKCategoryValueSleepAnalysisAsleepREM": "REM",
    "HKCategoryValueSleepAnalysisAwake": "Awake",
}
