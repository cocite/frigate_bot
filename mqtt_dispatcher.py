import asyncio
from aiomqtt import Client, MqttError
import log_config
from notifier import handle_review, start_delivery_workers, cleanup_worker

# Handlers are set up by log_config.setup() in __main__; console level comes from config.LOG_LEVEL
logger = log_config.get_logger("mqtt_dispatcher")

MQTT_QUEUE_SIZE = 100    # messages per topic queue; when full, the MQTT reader waits (backpressure)

# Task setup: each task has a topic, a handler and a number of workers.
# Several tasks may listen to the same topic, each with its own queue.
tasks_config = [
    #{
    #    "name": "frigate_alert_parallel",
    #    "topic": "frigate/reviews",
    #    "handler": send_something,
    #    "workers": 3
    #},
    {
        "name": "frigate_alert_sequential",
        "topic": "frigate/reviews",
        "handler": handle_review,
        "workers": 1
    }
]

async def worker(queue: asyncio.Queue, task_name: str, handler, worker_id: int):
    """Takes messages from the queue and runs the handler; a handler error is logged and does not kill the worker."""
    logger.info(f"[{task_name}] worker {worker_id} started")
    while True:
        msg = await queue.get()
        topic = str(msg.topic)
        payload = msg.payload.decode('utf-8', errors='ignore')

        try:
            await handler(payload)
            logger.debug(f"[{task_name}] worker {worker_id} finished '{topic}'")
        except Exception:
            logger.exception(f"[{task_name}] handler failed for '{topic}'")
        finally:
            queue.task_done()

async def mqtt_dispatcher(client, queues_by_topic):
    """Routes incoming MQTT messages to the queues of their topic."""
    async for message in client.messages:
        topic = str(message.topic)
        queues = queues_by_topic.get(topic, [])
        if queues:
            for queue in queues:
                if queue.full():
                    logger.warning(f"Queue for '{topic}' is full ({queue.qsize()}) — handlers can't keep up, waiting for a slot")
                await queue.put(message)
                logger.debug(f"Message on '{topic}' enqueued")
        else:
            logger.warning(f"No handlers for topic '{topic}'")

async def mqtt_client(queues_by_topic):
    """Keeps the MQTT connection alive: subscribes to all topics and reconnects on errors."""
    reconnect_interval = 5
    topics = set(queues_by_topic.keys())

    while True:
        try:
            async with Client("mosquitto", port=1883) as client:
                await client.subscribe([(topic, 0) for topic in topics])
                logger.info(f"Subscribed to {sorted(topics)}")

                await mqtt_dispatcher(client, queues_by_topic)

        except MqttError as e:
            logger.error(f"MQTT error: {e} — reconnecting in {reconnect_interval}s")
            await asyncio.sleep(reconnect_interval)

async def run_dispatcher():
    """Dispatcher core: queues, workers, MQTT loop."""
    queues_by_topic = {}
    workers_tasks = []
    for task_conf in tasks_config:
        queue = asyncio.Queue(maxsize=MQTT_QUEUE_SIZE)
        topic, handler = task_conf["topic"], task_conf["handler"]
        task_name, num_workers = task_conf["name"], task_conf["workers"]
        queues_by_topic.setdefault(topic, []).append(queue)
        for worker_id in range(num_workers):
            worker_task = asyncio.create_task(worker(queue, task_name, handler, worker_id))
            workers_tasks.append(worker_task)

    mqtt_task = asyncio.create_task(mqtt_client(queues_by_topic))

    logger.info("Dispatcher started, waiting for messages")
    await asyncio.gather(mqtt_task, *workers_tasks)

async def main():
    """Entry point: MQTT dispatcher, channel delivery workers, periodic cleanup.
    Client lifecycles (the MTProto session) are owned by the channel workers themselves.
    Everything in one gather: if any task dies, the process crashes with a traceback
    (compose restarts it) instead of failing silently."""
    await asyncio.gather(
        run_dispatcher(),
        cleanup_worker(),
        *start_delivery_workers(),
    )

if __name__ == "__main__":
    log_config.setup("frigate_bot.log")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Dispatcher stopped (Ctrl+C)")