# Package features.license
from .license_manager import (
    get_license_info,
    get_current_tier,
    get_allowed_features,
    is_feature_allowed,
    is_path_allowed,
    get_redirect_for_forbidden_path,
    activate_license_key,
    generate_license_key,
    TIER_INFO,
)

