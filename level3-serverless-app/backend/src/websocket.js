const {
  DynamoDBClient,
  PutItemCommand,
} = require("@aws-sdk/client-dynamodb");
const {
  ApiGatewayManagementApiClient,
  PostToConnectionCommand,
} = require("@aws-sdk/client-apigatewaymanagementapi");

const client = new DynamoDBClient({});

const TABLE_NAME = process.env.TABLE_NAME;

const sendAcknowledgement = async (event, acknowledgement) => {
  const requestContext = event?.requestContext ?? {};
  const { connectionId, domainName, stage } = requestContext;

  if (
    typeof acknowledgement.requestId !== "string" ||
    !acknowledgement.requestId ||
    !connectionId ||
    !domainName ||
    !stage
  ) {
    return false;
  }

  try {
    const managementClient = new ApiGatewayManagementApiClient({
      endpoint: `https://${domainName}/${stage}`,
    });

    await managementClient.send(
      new PostToConnectionCommand({
        ConnectionId: connectionId,
        Data: Buffer.from(JSON.stringify(acknowledgement)),
      })
    );
    return true;
  } catch (error) {
    if (error?.name === "GoneException" || error?.$metadata?.httpStatusCode === 410) {
      console.info("WebSocket connection is no longer active:", connectionId);
    } else {
      console.error("Unable to deliver WebSocket acknowledgement:", error);
    }
    return false;
  }
};

exports.connect = async (event) => {
  console.log("WebSocket client connected:", event.requestContext.connectionId);

  return {
    statusCode: 200,
    body: "Connected",
  };
};

exports.disconnect = async (event) => {
  console.log(
    "WebSocket client disconnected:",
    event.requestContext.connectionId
  );

  return {
    statusCode: 200,
    body: "Disconnected",
  };
};

exports.message = async (event) => {
  let payload;
  try {
    payload = JSON.parse(event.body);
  } catch {
    return {
      statusCode: 400,
      body: "Message must not be empty",
    };
  }

  const message = payload?.message;
  const requestId = payload?.requestId;
  if (
    typeof message !== "string" ||
    message.trim().length === 0 ||
    typeof requestId !== "string" ||
    requestId.trim().length === 0
  ) {
    await sendAcknowledgement(event, {
      type: "message_ack",
      requestId,
      status: "failed",
      error: "Message and requestId are required.",
    });

    return {
      statusCode: 400,
      body: "Message and requestId are required",
    };
  }

  console.log("Received WebSocket message:", message);

  try {
    await client.send(
      new PutItemCommand({
        TableName: TABLE_NAME,
        Item: {
          connectionId: {
            S: event.requestContext.connectionId,
          },
          message: {
            S: message,
          },
          createdAt: {
            S: new Date().toISOString(),
          },
        },
      })
    );
  } catch (error) {
    console.error("Unable to save WebSocket message:", error);
    await sendAcknowledgement(event, {
      type: "message_ack",
      requestId,
      status: "failed",
      error: "The message could not be saved.",
    });

    return {
      statusCode: 500,
      body: "Message could not be saved",
    };
  }

  const acknowledgementDelivered = await sendAcknowledgement(event, {
    type: "message_ack",
    requestId,
    status: "saved",
  });

  return {
    statusCode: 200,
    body: acknowledgementDelivered
      ? "Message saved and acknowledged"
      : "Message saved; acknowledgement unavailable",
  };
};