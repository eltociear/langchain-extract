from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from fastapi import HTTPException
from jsonschema import Draft202012Validator, exceptions
from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.prompts.chat import MessageLikeRepresentation
from langchain_core.runnables import chain
from langserve import CustomUserType
from pydantic import BaseModel, Field, validator
from typing_extensions import TypedDict

from db.models import QueryAnalysisExample as DBQueryAnalysisExample
from db.models import QueryAnalyzer
from extraction.utils import convert_json_schema_to_openai_schema
from server.settings import get_model
from server.validators import validate_json_schema

# Instantiate the model
model = get_model()


class QueryAnalysisExample(BaseModel):
    """An example query analysis.

    This example consists of input messages and the expected queries.
    """

    messages: List[AnyMessage] = Field(..., description="The input messages")
    output: List[Dict[str, Any]] = Field(
        ..., description="The expected output of the example. A list of objects."
    )


class QueryAnalysisRequest(CustomUserType):
    """Request body for the query analyzer endpoint."""

    messages: List[AnyMessage] = Field(
        ..., description="The messages to generates queries from."
    )
    json_schema: Dict[str, Any] = Field(
        ...,
        description="JSON schema that describes what a query looks like",
        alias="schema",
    )
    instructions: Optional[str] = Field(
        None, description="Supplemental system instructions."
    )
    examples: Optional[List[QueryAnalysisExample]] = Field(
        None, description="Examples of optimized queries."
    )

    @validator("json_schema")
    def validate_schema(cls, v: Any) -> Dict[str, Any]:
        """Validate the schema."""
        validate_json_schema(v)
        return v


class QueryAnalysisResponse(TypedDict):
    """Response body for the query analysis endpoint."""

    data: List[Any]


def _deduplicate(
    responses: Sequence[QueryAnalysisResponse],
) -> QueryAnalysisResponse:
    """Deduplicate the results.

    The deduplication is done by comparing the serialized JSON of each of the results
    and only keeping the unique ones.
    """
    unique = []
    seen = set()
    for response in responses:
        for data_item in response["data"]:
            # Serialize the data item for comparison purposes
            serialized = json.dumps(data_item, sort_keys=True)
            if serialized not in seen:
                seen.add(serialized)
                unique.append(data_item)

    return {
        "data": unique,
    }


def _cast_example_to_dict(example: DBQueryAnalysisExample) -> Dict[str, Any]:
    """Cast example record to dictionary."""
    return {
        "messages": example.content,
        "output": example.output,
    }


def _make_prompt_template(
    instructions: Optional[str],
    examples: Optional[Sequence[QueryAnalysisExample]],
    function_name: str,
) -> ChatPromptTemplate:
    """Make a system message from instructions and examples."""
    prefix = (
        "You are a world class expert at converting user questions into database "
        "queries. Given a question, return a list of database queries optimized to "
        "retrieve the most relevant results."
    )
    if instructions:
        system_message = ("system", f"{prefix}\n\n{instructions}")
    else:
        system_message = ("system", prefix)
    prompt_components: List[MessageLikeRepresentation] = [system_message]
    if examples is not None:
        for example in examples:
            # TODO: We'll need to refactor this at some point to
            # support other encoding strategies. The function calling logic here
            # has some hard-coded assumptions (e.g., name of parameters like `data`).
            function_call = {
                "arguments": json.dumps(
                    {
                        "data": example.output,
                    }
                ),
                "name": function_name,
            }
            prompt_components.extend(
                [
                    *example.messages,
                    AIMessage(
                        content="", additional_kwargs={"function_call": function_call}
                    ),
                ]
            )

    prompt_components.append(MessagesPlaceholder("input"))
    return ChatPromptTemplate.from_messages(prompt_components)


# PUBLIC API


def get_examples_from_query_analyzer(
    query_analyzer: QueryAnalyzer,
) -> List[Dict[str, Any]]:
    """Get examples from an query_analyzer."""
    return [_cast_example_to_dict(example) for example in query_analyzer.examples]


@chain
async def query_analyzer(request: QueryAnalysisRequest) -> QueryAnalysisResponse:
    """An end point to generate queries from a list of messages."""
    # TODO: Add validation for model context window size
    schema = request.json_schema
    try:
        Draft202012Validator.check_schema(schema)
    except exceptions.ValidationError as e:
        raise HTTPException(status_code=422, detail=f"Invalid schema: {e.message}")

    openai_function = convert_json_schema_to_openai_schema(schema)
    function_name = openai_function["name"]
    prompt = _make_prompt_template(
        request.instructions,
        request.examples,
        function_name,
    )
    runnable = prompt | model.with_structured_output(openai_function)

    return await runnable.ainvoke({"input": request.messages})