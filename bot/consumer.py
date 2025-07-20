import json
import os
import time
from kafka import KafkaConsumer
from hdfs import InsecureClient

# Load env vars
KAFKA_TOPIC = os.getenv('KAFKA_TOPIC', 'discord-topic')
KAFKA_BROKER = os.getenv('KAFKA_BROKER', 'kafka:9092')
HDFS_URL = os.getenv('HDFS_URL', 'http://namenode:9870')
HDFS_PATH = os.getenv('HDFS_PATH', '/user/hdfs/discord-data.jsonl')

# --- Step 1: Wait for Kafka to be ready ---
consumer = None
for i in range(10):
    try:
        consumer = KafkaConsumer(
            KAFKA_TOPIC,
            bootstrap_servers=KAFKA_BROKER,
            auto_offset_reset='earliest',
            enable_auto_commit=True,
            group_id='discord-consumer-group',
            value_deserializer=lambda m: json.loads(m.decode('utf-8'))
        )
        print("[✅] Connected to Kafka")
        break
    except Exception as e:
        print(f"[⏳] Kafka not ready yet ({i+1}/10): {e}")
        time.sleep(5)

if consumer is None:
    raise Exception("❌ Failed to connect to Kafka after multiple attempts")

# --- Step 2: Connect to HDFS ---
client = InsecureClient(HDFS_URL, user='hdfs')
print("[✅] Connected to HDFS")

# --- Step 3: Start consuming messages ---
print(f"[📥] Listening to topic: {KAFKA_TOPIC}")
while True:
    try:
        for msg in consumer:
            message = msg.value
            json_line = json.dumps(message) + "\n"
            with client.write(HDFS_PATH, encoding='utf-8', append=True) as writer:
                writer.write(json_line)
            print(f"[📤] Written to HDFS: {json_line.strip()}")
    except Exception as err:
        print(f"[⚠️] Error in processing: {err}")
        time.sleep(3)
