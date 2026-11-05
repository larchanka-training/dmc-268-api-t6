import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { SwapiClient } from "./api-client.js";
import { loadConfig, type Config } from "./config.js";
import { registerFilmTools } from "./tools/films.js";

export const SERVER_NAME = "swapi-mcp";
export const SERVER_VERSION = "1.0.0";

export function createServer(config: Config = loadConfig()): McpServer {
  const server = new McpServer({ name: SERVER_NAME, version: SERVER_VERSION });
  const client = new SwapiClient(config);
  registerFilmTools(server, client);
  return server;
}
