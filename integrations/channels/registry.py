"""Lazy channel loading keeps optional SDKs optional."""

from importlib import import_module

from integrations.config import CHANNELS


class ChannelRegistry:
    def __init__(self, config, clients=None):
        self.config = config
        self.clients = clients or {}

    def get(self, channel):
        if channel not in CHANNELS:
            raise ValueError("unsupported channel")
        if channel not in self.clients:
            module = import_module("integrations.channels." + channel)
            self.clients[channel] = module.Client(self.config)
        return self.clients[channel]
