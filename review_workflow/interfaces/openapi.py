"""OpenAPI contract for the review workflow HTTP interface."""

from __future__ import annotations

from typing import Any, Dict


def openapi_schema() -> Dict[str, Any]:
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Stock Review Workflow API",
            "version": "1.0.0",
            "description": "可恢复的持股复盘 Agent Workflow API",
        },
        "servers": [{"url": "/"}],
        "paths": {
            "/api/health": {
                "get": {
                    "operationId": "health",
                    "responses": {"200": {"description": "服务健康"}},
                }
            },
            "/api/review-runs": {
                "post": {
                    "operationId": "createReviewRun",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/CreateRunRequest"}
                            }
                        },
                    },
                    "responses": {
                        "201": {"description": "已创建"},
                        "202": {"description": "已创建并后台执行"},
                    },
                }
            },
            "/api/review-runs/{date}": {
                "get": {
                    "operationId": "getReviewRun",
                    "parameters": [
                        {
                            "name": "date",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "format": "date"},
                        }
                    ],
                    "responses": {"200": {"description": "工作流状态"}},
                }
            },
            "/api/review-runs/{date}/steps": {
                "post": {
                    "operationId": "stepReviewRun",
                    "parameters": [
                        {
                            "name": "date",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "format": "date"},
                        }
                    ],
                    "requestBody": {
                        "required": False,
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    },
                    "responses": {"200": {"description": "执行一个原子步骤"}},
                }
            },
            "/api/review-runs/{date}/resume": {
                "post": {
                    "operationId": "resumeReviewRun",
                    "parameters": [
                        {
                            "name": "date",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "format": "date"},
                        }
                    ],
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ResumeRequest"}
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "已恢复"},
                        "202": {"description": "已恢复并后台执行"},
                    },
                }
            },
            "/api/review-runs/{date}/render": {
                "post": {
                    "operationId": "renderReviewRun",
                    "parameters": [
                        {
                            "name": "date",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "format": "date"},
                        }
                    ],
                    "responses": {"200": {"description": "最终文档已重渲染"}},
                }
            },
            "/api/review-runs/{date}/document": {
                "get": {
                    "operationId": "getReviewDocument",
                    "parameters": [
                        {
                            "name": "date",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "format": "date"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "最终 Markdown",
                            "content": {"text/markdown": {"schema": {"type": "string"}}},
                        }
                    },
                }
            },
        },
        "components": {
            "schemas": {
                "CreateRunRequest": {
                    "type": "object",
                    "required": ["date"],
                    "properties": {
                        "date": {"type": "string", "format": "date"},
                        "provider": {"type": "string", "enum": ["auto", "gemini", "zhipu"]},
                        "model": {"type": "string"},
                        "search_provider": {
                            "type": "string",
                            "enum": ["auto", "zhipu", "model", "none"],
                        },
                        "timeout": {"type": "number", "minimum": 5, "maximum": 3600},
                        "device_scale_factor": {"type": "number", "minimum": 0.5, "maximum": 4},
                        "headed": {"type": "boolean"},
                        "skip_capture": {"type": "boolean"},
                        "recapture": {"type": "boolean"},
                        "no_web_search": {"type": "boolean"},
                        "no_peer_capture": {"type": "boolean"},
                        "no_portfolio_summary": {"type": "boolean"},
                        "board": {"type": "array", "items": {"type": "string"}},
                        "peer_stock": {"type": "array", "items": {"type": "string"}},
                        "holdings": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"$ref": "#/components/schemas/HoldingInput"},
                        },
                        "force": {"type": "boolean"},
                        "execute": {"type": "boolean"},
                    },
                },
                "HoldingInput": {
                    "type": "object",
                    "required": ["name", "cost", "shares", "plan"],
                    "properties": {
                        "name": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 40,
                            "description": "股票名称或代码",
                        },
                        "cost": {"type": "number", "exclusiveMinimum": 0},
                        "shares": {"type": "integer", "minimum": 1},
                        "plan": {
                            "type": "string",
                            "enum": [
                                "3个月内",
                                "6个月内",
                                "1年之内",
                                "3年之内",
                                "5年之内",
                            ],
                        },
                    },
                },
                "ResumeRequest": {
                    "type": "object",
                    "properties": {
                        "retry_failed": {"type": "boolean", "default": True},
                        "execute": {"type": "boolean", "default": False},
                    },
                },
            }
        },
    }
