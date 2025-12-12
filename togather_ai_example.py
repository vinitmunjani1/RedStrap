from together import Together

client = Together() # auth defaults to os.environ.get("TOGETHER_API_KEY")

response = client.chat.completions.create(
    model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
    messages=[
      {
        "role": "user",
        "content": "What are some fun things to do in New York?"
      }
    ]
)
print(response.choices[0].message.content)