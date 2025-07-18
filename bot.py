import discord
from kafka import KafkaProducer
import json
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
KAFKA_BROKER = os.getenv('KAFKA_BROKER', 'localhost:9092')
KAFKA_TOPIC = os.getenv('KAFKA_TOPIC', 'discord-topic')

# Discord client setup
intents = discord.Intents.default()
intents.messages = True
client = discord.Client(intents=intents)

# Kafka producer setup
producer = KafkaProducer(
    bootstrap_servers=KAFKA_BROKER,
    value_serializer=lambda v: json.dumps(v).encode('utf-8')
)

@client.event
async def on_ready():
    print(f'Bot connected as {client.user}')

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    data = {
        'user': str(message.author),
        'channel': str(message.channel),
        'content': message.content,
        'timestamp': str(message.created_at)
    }

    producer.send(KAFKA_TOPIC, value=data)
    print(f'Sent to Kafka: {data}')

client.run(DISCORD_TOKEN)
