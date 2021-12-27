import os
from os import environ
import json
import requests
import concurrent.futures
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from datetime import datetime
from pytz import timezone
from utils import expand_config_template
from storage import save_file
from notifier import notify
from envoy import update_percentage_fault, update_duration_fault
from logger import get_logger

logger = get_logger('app')

requestsSesstion = requests.Session()
retries = Retry(total=10,
                backoff_factor=0.1,
                status_forcelist=[ 500, 502, 503, 504 ])
requestsSesstion.mount('http://', HTTPAdapter(max_retries=retries, pool_maxsize=500))

TIME_ZONE = environ.get('TIME_ZONE')

if TIME_ZONE:
    curr_time = datetime.now().astimezone(timezone(TIME_ZONE))
else:
    curr_time = datetime.now()

test_id = curr_time.strftime('%a %b %d %Hh%Mm%Ss %Y')

def build_scenarios():
    conf_file = open(os.environ.get('CONFIG_FILE'), 'r')
    conf = json.load(conf_file)

    fault_percentages = conf['fault']['percentage']
    fault_duration = conf['fault']['duration']
    patterns = conf['patterns']
    workloads = conf['concurrentUsers']
    rounds = conf['rounds'] + 1
    max_requests_allowed = conf['maxRequestsAllowed']
    target_successful_requests = conf['targetSuccessfulRequests']
    all_scenarios = []
    scenario_groups = {}

    for fault_percentage in fault_percentages:
        for workload in workloads:
            scenarios = []
            for pattern_template in patterns:
                config_templates = expand_config_template(pattern_template['configTemplate'])
                for config_template in config_templates:
                    for idx_round in range(1, rounds):
                        scenarios.append({
                            'configTemplate': config_template,
                            'patternTemplate': pattern_template,
                            'users': workload,
                            'round': idx_round,
                            'faultPercentage': fault_percentage,
                            'faultDuration': fault_duration,
                            'maxRequestsAllowed': max_requests_allowed,
                            'targetSuccessfulRequests': target_successful_requests
                        })
            scenario_group_id = 'f'+str(fault_percentage)+'u'+str(workload)
            scenario_groups[scenario_group_id] = scenarios
            all_scenarios += scenarios

    logger.info(f'{len(all_scenarios)} scenarios generated')
    save_file(f'{test_id}/scenarios', all_scenarios, 'json')
    return scenario_groups

def main():
    scenario_groups = build_scenarios()
    all_results = []
    curr_fault_perc = ''
    curr_fault_duration = ''

    for scenario_group in scenario_groups.keys():
        logger.info(f'Starting scenario group {scenario_group} with {len(scenario_groups[scenario_group])} scenario(s)')
        results = []
        scenario_group_count = 0

        for scenario in scenario_groups[scenario_group]:
            scenario_group_count += 1
            logger.info(f'Processing scenario {scenario_group_count}/{len(scenario_groups[scenario_group])}')
            users = scenario['users'] + 1

            if scenario['faultPercentage'] != curr_fault_perc:
                curr_fault_perc = scenario['faultPercentage']
                update_percentage_fault(int(curr_fault_perc))

            if scenario['faultDuration'] != curr_fault_duration:
                curr_fault_duration = scenario['faultDuration']
                update_duration_fault(int(curr_fault_duration))

            with concurrent.futures.ThreadPoolExecutor(max_workers=users) as executor:
                futures = []
                for user_id in range(1, users):
                    futures.append(executor.submit(do_test, scenario=scenario, user_id=user_id))
                
                totResults = 0
                for future in concurrent.futures.as_completed(futures):
                    results.append(future.result())
                    totResults += 1
                    logger.info(f'TotResults: {totResults}')

        all_results += results
        save_file(f'{test_id}/results_{scenario_group}', results, 'csv')
            
    save_file(f'{test_id}/results', all_results, 'csv')
    notify(f'{test_id} - done!')

def do_test(scenario, user_id):
    start_time = datetime.now()
    users = scenario['users']
    pattern_template = scenario['patternTemplate']
    fault_percentage = scenario['faultPercentage']
    fault_duration = scenario['faultDuration']
    config_template = scenario['configTemplate']
    idx_round = scenario['round']
    maxRequestsAllowed = scenario['maxRequestsAllowed']
    targetSuccessfulRequests = scenario['targetSuccessfulRequests']

    payload = json.dumps({
        'maxRequestsAllowed': maxRequestsAllowed,
        'targetSuccessfulRequests': targetSuccessfulRequests,
        'params': config_template
    })
    response = requestsSesstion.post(
        pattern_template['url'], 
        data=payload, 
        headers={'Content-Type': 'application/json'}
    )
    result = {}
    if response.status_code == 200:
        result = response.json()
        result['userId'] = user_id
        result['startTime'] = start_time
        result['endTime'] = datetime.now()
        result['users'] = users
        result['round'] = idx_round
        result['lib'] = pattern_template['lib']
        result['pattern'] = pattern_template['pattern']
        result['faultPercentage'] = fault_percentage
        result['faultDuration'] = fault_duration
        for config_key in config_template.keys():
            result[config_key] = config_template[config_key]
    else:
        result['error'] = True
        result['errorMessage'] = response.text
        result['statusCode'] = response.status_code
    
    return result
    
main()
