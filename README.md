# swf-transform

A worker trasnformer used to process STF slice messages in worker nodes and forward results into the Panda pipeline.

This repository contains the transformer that receives STF slice messages from the queue, performs the required payload processing, publishes the results, and acknowledges the original message so the pipeline can continue.

## Table of contents

- [Overview](#overview)
- [Message flow](#message-flow)
- [Contract (inputs / outputs)](#contract-inputs--outputs)
- [Configuration](#configuration)
- [Running (deployment notes)](#running-deployment-notes)
- [Development](#development)
- [Contributing](#contributing)
- [License](#license)

## Overview

The transformer runs on worker nodes and implements the processing loop for STF slices. Its responsibilities are:

- Receive a message containing an STF slice.
- Process the payload according to the slice instructions (parsing, transforming, validating, etc.).
- Publish the processing results to the results topic.
- Acknowledge the original message so the broker can dispatch the next slice.

## Message flow

Typical message flow used by this wrapper:

1. The transformer receives an STF slice message from the input queue: `/queue/panda.slices.transformer`.
2. The transformer processes the payload based on the STF slice message contents.
3. The transformer sends the processing results to the results topic: `/topic/panda.results`.
4. The transformer acknowledges the original message so the broker can deliver the next message.

> Note: queue and topic names above reflect the current project conventions. If your broker or topology differs, update configuration accordingly.

## Contract (inputs / outputs)

- Input: STF slice messages (queue: `/queue/panda.slices.transformer`). The message payload is expected to contain all metadata and references required for processing.
- Output: Result messages posted to `/topic/panda.results`. Results should contain status, any produced artifacts or references, and diagnostics if processing failed.
- Error handling: failures should be reported to results with an error status, and messages should only be acknowledged (ack/nack) after the worker has reliably produced a result (success or failure) or after configured retry/poison-queue handling.

## Configuration

This transformer will access PanDA/iDDS to get the ActiveMQ configuration.

## Running notes

(todo)
(Migrated from iDDS/prompt. More updates to do to make the migration run correctly)

## License

This project is licensed under the terms in the `LICENSE` file in this repository.
