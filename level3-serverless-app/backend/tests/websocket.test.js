const mockDynamoSend = jest.fn();
const mockManagementSend = jest.fn();

jest.mock("@aws-sdk/client-dynamodb", () => ({
  DynamoDBClient: jest.fn().mockImplementation(() => ({
    send: mockDynamoSend,
  })),
  PutItemCommand: jest.fn().mockImplementation((input) => ({ input })),
}));

jest.mock("@aws-sdk/client-apigatewaymanagementapi", () => ({
  ApiGatewayManagementApiClient: jest.fn().mockImplementation(() => ({
    send: mockManagementSend,
  })),
  PostToConnectionCommand: jest.fn().mockImplementation((input) => ({ input })),
}));

process.env.TABLE_NAME = "test-table";

const { connect, disconnect, message } = require("../src/websocket");
const { PutItemCommand } = require("@aws-sdk/client-dynamodb");
const {
  ApiGatewayManagementApiClient,
  PostToConnectionCommand,
} = require("@aws-sdk/client-apigatewaymanagementapi");

const makeMessageEvent = (payload) => ({
  body: JSON.stringify(payload),
  requestContext: {
    connectionId: "test-connection-1",
    domainName: "abc123.execute-api.ap-southeast-2.amazonaws.com",
    stage: "dev",
  },
});

describe("WebSocket Lambda handlers", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockDynamoSend.mockResolvedValue({});
    mockManagementSend.mockResolvedValue({});
  });

  test("connect handler returns 200", async () => {
    const event = {
      requestContext: {
        connectionId: "test-connection-1",
      },
    };

    const result = await connect(event);

    expect(result.statusCode).toBe(200);
    expect(result.body).toBe("Connected");
  });

  test("disconnect handler returns 200", async () => {
    const event = {
      requestContext: {
        connectionId: "test-connection-1",
      },
    };

    const result = await disconnect(event);

    expect(result.statusCode).toBe(200);
    expect(result.body).toBe("Disconnected");
  });

  test("message handler stores a valid message", async () => {
    const calls = [];
    mockDynamoSend.mockImplementationOnce(async () => {
      calls.push("dynamodb");
    });
    mockManagementSend.mockImplementationOnce(async () => {
      calls.push("acknowledgement");
    });

    const result = await message(
      makeMessageEvent({
        action: "message",
        requestId: "request-1",
        message: "Hello",
      })
    );

    expect(result.statusCode).toBe(200);
    expect(calls).toEqual(["dynamodb", "acknowledgement"]);
    expect(PutItemCommand.mock.calls[0][0]).toEqual({
      TableName: "test-table",
      Item: expect.objectContaining({
        connectionId: { S: "test-connection-1" },
        message: { S: "Hello" },
        createdAt: { S: expect.any(String) },
      }),
    });
    expect(ApiGatewayManagementApiClient).toHaveBeenCalledWith({
      endpoint: "https://abc123.execute-api.ap-southeast-2.amazonaws.com/dev",
    });
    expect(PostToConnectionCommand.mock.calls[0][0].ConnectionId).toBe(
      "test-connection-1"
    );
    expect(
      JSON.parse(PostToConnectionCommand.mock.calls[0][0].Data.toString())
    ).toEqual({
      type: "message_ack",
      requestId: "request-1",
      status: "saved",
    });
  });

  test.each([
    ["empty", ""],
    ["whitespace-only", "   "],
    ["non-string", 42],
    ["missing message", undefined],
  ])("message handler rejects %s content", async (_description, messageValue) => {
    const result = await message(
      makeMessageEvent({
        action: "message",
        requestId: "request-invalid",
        message: messageValue,
      })
    );

    expect(result.statusCode).toBe(400);
    expect(mockDynamoSend).not.toHaveBeenCalled();
    expect(PutItemCommand).not.toHaveBeenCalled();
    expect(
      JSON.parse(PostToConnectionCommand.mock.calls[0][0].Data.toString())
    ).toMatchObject({
      type: "message_ack",
      requestId: "request-invalid",
      status: "failed",
    });
  });

  test("message handler rejects malformed JSON without a write", async () => {
    const event = makeMessageEvent({ action: "message" });
    event.body = "{";

    const result = await message(event);

    expect(result.statusCode).toBe(400);
    expect(mockDynamoSend).not.toHaveBeenCalled();
    expect(PostToConnectionCommand).not.toHaveBeenCalled();
  });

  test("DynamoDB failure sends a failed acknowledgement", async () => {
    mockDynamoSend.mockRejectedValueOnce(new Error("DynamoDB unavailable"));

    const result = await message(
      makeMessageEvent({
        action: "message",
        requestId: "request-db-failure",
        message: "Hello",
      })
    );

    expect(result.statusCode).toBe(500);
    expect(mockManagementSend).toHaveBeenCalledTimes(1);
    expect(
      JSON.parse(PostToConnectionCommand.mock.calls[0][0].Data.toString())
    ).toMatchObject({
      requestId: "request-db-failure",
      status: "failed",
    });
    expect(
      JSON.parse(PostToConnectionCommand.mock.calls[0][0].Data.toString()).status
    ).not.toBe("saved");
  });

  test("stale connections do not throw after the DynamoDB write", async () => {
    const goneError = new Error("Connection is gone");
    goneError.name = "GoneException";
    goneError.$metadata = { httpStatusCode: 410 };
    mockManagementSend.mockRejectedValueOnce(goneError);
    const consoleInfo = jest.spyOn(console, "info").mockImplementation(() => {});

    const result = await message(
      makeMessageEvent({
        action: "message",
        requestId: "request-gone",
        message: "Hello",
      })
    );

    expect(result.statusCode).toBe(200);
    expect(mockDynamoSend).toHaveBeenCalledTimes(1);
    expect(consoleInfo).toHaveBeenCalled();
    expect(
      JSON.parse(PostToConnectionCommand.mock.calls[0][0].Data.toString()).status
    ).toBe("saved");
    consoleInfo.mockRestore();
  });

  test("acknowledgement errors do not claim the message failed to save", async () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => {});
    mockManagementSend.mockRejectedValueOnce(new Error("Management API unavailable"));

    const result = await message(
      makeMessageEvent({
        action: "message",
        requestId: "request-ack-failure",
        message: "Hello",
      })
    );

    expect(result.body).toBe("Message saved; acknowledgement unavailable");
    expect(mockDynamoSend).toHaveBeenCalledTimes(1);
    consoleError.mockRestore();
  });
});