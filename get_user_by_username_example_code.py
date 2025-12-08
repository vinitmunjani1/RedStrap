import requests

url = "https://twitter241.p.rapidapi.com/user"

querystring = {"username":"MrBeast"}

headers = {
	"x-rapidapi-key": "d409935002msh6ca472652d2ea6ep143efcjsn4d350435d328",
	"x-rapidapi-host": "twitter241.p.rapidapi.com"
}

response = requests.get(url, headers=headers, params=querystring)

print(response.json())