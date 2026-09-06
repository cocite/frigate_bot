import asyncio
import logging
from aiomqtt import Client, MqttError
import log_config
from notifier import send_frigate_alert, start_delivery_workers, cleanup_worker

# Хендлеры настраивает log_config.setup() в блоке __main__
logger = logging.getLogger("mqtt_dispatcher")
logger.setLevel(logging.DEBUG)

# Настройка задач:
# Каждая задача имеет: топик, обработчик и количество воркеров.
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
        "handler": send_frigate_alert,
        "workers": 1
    }
]

async def worker(queue: asyncio.Queue, task_name: str, handler, worker_id: int):
    logger.info(f"[{task_name}] Worker-{worker_id} started.")
    while True:
        msg = await queue.get()
        topic = str(msg.topic)
        payload = msg.payload.decode('utf-8', errors='ignore')

        logger.debug(f"[{task_name}] Worker-{worker_id} handling '{topic}'.")
        try:
            await handler(payload)
            logger.info(f"[{task_name}] Worker-{worker_id} successfully handled '{topic}'.")
        except Exception as e:
            logger.exception(f"[{task_name}] Error handling '{topic}': {e}")
        finally:
            queue.task_done()

async def mqtt_dispatcher(client, queues_by_topic):
    async for message in client.messages:
        topic = str(message.topic)
        queues = queues_by_topic.get(topic, [])
        if queues:
            for queue in queues:
                if queue.full():
                    logger.warning(f"Очередь '{topic}' заполнена — обработчики не успевают, приём ждёт.")
                await queue.put(message)
                logger.debug(f"Message on topic '{topic}' enqueued.")
        else:
            logger.warning(f"No queues for topic '{topic}'.")

async def mqtt_client(queues_by_topic):
    reconnect_interval = 5
    topics = set(queues_by_topic.keys())

    while True:
        try:
            async with Client("mosquitto", port=1883) as client:
                await client.subscribe([(topic, 0) for topic in topics])
                logger.info(f"Subscribed to topics: {list(topics)}")

                await mqtt_dispatcher(client, queues_by_topic)

        except MqttError as e:
            logger.error(f"MQTT Error '{e}'. Reconnecting in {reconnect_interval}s...")
            await asyncio.sleep(reconnect_interval)

async def run_dispatcher():
    """Ядро диспетчера: очереди, воркеры, MQTT-цикл."""
    queues_by_topic = {}
    workers_tasks = []
    for task_conf in tasks_config:
        queue = asyncio.Queue(maxsize=100)
        topic, handler = task_conf["topic"], task_conf["handler"]
        task_name, num_workers = task_conf["name"], task_conf["workers"]
        queues_by_topic.setdefault(topic, []).append(queue)
        for worker_id in range(num_workers):
            worker_task = asyncio.create_task(worker(queue, task_name, handler, worker_id))
            workers_tasks.append(worker_task)

    mqtt_task = asyncio.create_task(mqtt_client(queues_by_topic))

    logger.info("MQTT диспетчер запущен. Ожидание сообщений...")
    await asyncio.gather(mqtt_task, *workers_tasks)

async def main():
    """Entry point: воркеры доставки каналов, периодическая уборка, MQTT-диспетчер.
    Жизненным циклом клиентов (сессия MTProto) владеют сами воркеры каналов."""
    delivery_tasks = start_delivery_workers()          # держим ссылки, иначе соберёт GC
    cleanup_task = asyncio.create_task(cleanup_worker(), name="cleanup")
    await run_dispatcher()

if __name__ == "__main__":
    log_config.setup("frigate_bot.log")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("MQTT Dispatcher stopped manually.")