"""AI-first report failure diagnosis with explicit rule fallback metadata."""

from __future__ import annotations

from typing import Any

from .diagnosis_quality import calibrate_ai_result, public_provider_error


def analyze_failure_with_ai(
    test_name: str,
    error_message: str,
    stack_trace: str,
    module: str,
    class_names: list[str] | None,
    *,
    analyzer_factory: Any,
    parse_failure_info: Any,
    rule_based_analysis: Any,
    config_manager: Any,
    stack_trace_utils: Any,
    logger: Any,
) -> dict[str, Any]:
    class_names = class_names or []
    failure_location = stack_trace_utils.extract_failure_location(stack_trace)
    if failure_location:
        logger.info(
            "从堆栈提取失败位置: %s.%s:%s",
            failure_location['file_name'],
            failure_location['file_type'],
            failure_location['line_number'],
        )

    ai_error = ''
    preferred_provider = ''
    attempted_providers = []
    try:
        if analyzer_factory is None:
            raise RuntimeError("Universal AI analyzer is not configured")
        analyzer = analyzer_factory()
        preferred_provider = analyzer.get_local_provider() or ''
        failure_info = parse_failure_info(test_name, error_message)
        result = analyzer.analyze_test_failure(
            class_name=failure_info.get('class_name', ''),
            method_name=failure_info.get('method_name'),
            error_message=error_message,
            stack_trace=stack_trace,
            auto_fetch_source=True,
            preferred_provider=preferred_provider or None,
        )
        attempted_providers = result.get('attempted_providers') or []
        if result['success']:
            provider_name = result.get('provider', 'unknown')
            provider_config = config_manager.get_ai_provider_config(provider_name)
            provider_display = (
                provider_config.get('name', f'{provider_name.upper()} AI')
                if provider_config else f'{provider_name.upper()} AI'
            )
            raw_provider_errors = result.get('provider_errors') or []
            safe_provider_errors = [
                public_provider_error(error) for error in raw_provider_errors
            ]
            if raw_provider_errors:
                logger.warning("AI provider 降级: %s", "; ".join(safe_provider_errors))
            response = {
                'root_cause': result.get('root_cause', ''),
                'analysis': result.get('analysis', ''),
                'suggestions': result.get('suggestions', []),
                'solution': result.get('solution'),
                'ai_enabled': True,
                'ai_model': provider_display,
                'ai_provider': provider_name,
                'ai_fallback_used': bool(result.get('fallback_used')),
                'ai_preferred_provider': result.get('preferred_provider', ''),
                'ai_providers_attempted': attempted_providers,
                'ai_provider_errors': safe_provider_errors,
                'root_cause_evidence': result.get('evidence') or [],
                'stack_trace': stack_trace,
            }
            source_info = result.get('source_info')
            if source_info:
                response.update({
                    'source_code_fetched': True,
                    'source_file_path': source_info.get('file_path', ''),
                    'source_url': source_info.get('url', ''),
                    'source_project': source_info.get('project', ''),
                })
                logger.info(
                    "成功获取源码信息: %s",
                    source_info.get('file_path', 'unknown'),
                )
            return calibrate_ai_result(response, error_message, stack_trace)
        ai_error = public_provider_error(result.get('error') or 'AI分析失败')
        logger.warning("AI分析失败: %s", ai_error)
    except ImportError:
        ai_error = '通用AI分析器未安装'
        logger.warning("通用AI分析器未安装，使用基于规则的分析")
    except Exception as exc:
        ai_error = public_provider_error(exc)
        logger.warning("通用AI分析失败: %s，使用基于规则的分析", ai_error)

    fallback = rule_based_analysis(
        test_name, error_message, stack_trace, module
    )
    fallback.update({
        'ai_attempted': True,
        'ai_error': public_provider_error(
            ai_error or '所有已配置 AI provider 均不可用'
        ),
        'ai_provider': preferred_provider,
        'ai_providers_attempted': attempted_providers,
    })
    return calibrate_ai_result(fallback, error_message, stack_trace)
