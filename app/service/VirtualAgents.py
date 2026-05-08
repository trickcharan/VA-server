import os
import json

from app.model.VirtualAgentInfo import VirtualAgentInfo

CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config')


class VirtualAgents:

    def __init__(self):
        self.virtual_agent_info = []
        self._load_virtual_agents()

    def get_all_ai_agent(self):
        return self.virtual_agent_info

    def _load_virtual_agents(self):
        with open(os.path.join(CONFIG_DIR, 'virtual_agents.json'), 'r') as file:
            data = json.load(file)
        for item in data:
            self.virtual_agent_info.append(
                VirtualAgentInfo(
                    virtual_agent_id=item['virtual_agent_id'],
                    virtual_agent_name=item['virtual_agent_name'],
                    is_default=item['is_default'],
                )
            )
