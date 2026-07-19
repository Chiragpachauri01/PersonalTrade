"""Exception hierarchy. Every domain error derives from PersonalTradeError."""


class PersonalTradeError(Exception):
    """Base class for all PersonalTrade domain errors."""


class ConfigError(PersonalTradeError):
    """Configuration is missing, malformed, or fails validation."""
