import grpc
import logging
import os
from concurrent import futures

from downstream.proto import byova_common_pb2
from downstream.proto import voicevirtualagent_pb2_grpc
from downstream.service.VirtualAgents import VirtualAgents
from downstream.interceptor.AuthInterceptor import AuthInterceptor
from downstream.service.RequestProcessor import RequestProcessor

PORT = 8086


class AIAgent(voicevirtualagent_pb2_grpc.VoiceVirtualAgentServicer):
    def __init__(self):
        super().__init__()
        self.ai_agent = VirtualAgents()
        self.state = dict()

    def _get_tracking_id(self, context):
        for meta_data in context.invocation_metadata():
            if meta_data.key == 'trackingid':
                return meta_data.value
        return ""

    def ListVirtualAgents(self, request, context):
        try:
            for meta_data in context.invocation_metadata():
                if meta_data.key == 'trackingid':
                    break
            response = byova_common_pb2.ListVAResponse()
            for agent in self.ai_agent.get_all_ai_agent():
                virtual_agent_info = response.virtual_agents.add()
                virtual_agent_info.virtual_agent_id = str(agent.virtual_agent_id)
                virtual_agent_info.virtual_agent_name = agent.virtual_agent_name
                virtual_agent_info.is_default = agent.is_default
            print(f"Returning List VA response")
            return response
        except Exception as ex:
            print(f"Error in ListVirtualAgents: {ex}")
            raise

    def ProcessCallerInput(self, request_iterator, context):
        conversation_id = None
        tracking_id = self._get_tracking_id(context)
        try:
            for request in request_iterator:
                conversation_id = request.conversation_id
                if request.conversation_id not in self.state:
                    self.state[request.conversation_id] = RequestProcessor(
                        request.conversation_id,
                        request.virtual_agent_id,
                        tracking_id,
                    )
                yield from self.state[request.conversation_id].process_request(request)
        except grpc.RpcError as e:
            print(f"[{conversation_id}] gRPC error: {e}")
        except Exception as ex:
            print(f"[{conversation_id}] Error: {ex}")
        finally:
            # gRPC stream ended — Webex CC may open multiple streams for the
            # same conversation_id, so do NOT cleanup the adapter here.
            # Cleanup happens when SESSION_END event is received by RequestProcessor.
            if conversation_id:
                processor = self.state.get(conversation_id)
                can_delete = processor and processor.can_be_deleted
                if can_delete:
                    del self.state[conversation_id]
                print(f"[{conversation_id}] Stream ended (deleted={can_delete})")


def serve():
    thread_count = int(os.environ.get('worker_thread', 10))
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=thread_count),
        #interceptors=[AuthInterceptor()],
    )
    voicevirtualagent_pb2_grpc.add_VoiceVirtualAgentServicer_to_server(AIAgent(), server)
    server.add_insecure_port(f'[::]:{PORT}')
    print('starting server')
    server.start()
    server.wait_for_termination()


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass  # dotenv not required in production (env vars injected by container)
    serve()
