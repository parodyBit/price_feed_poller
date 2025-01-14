#!/usr/bin/env python3
import argparse
import contextlib
import datetime
import os
import subprocess
import sys
import time

from configs import load_price_feeds_config, load_version
from contracts import wpr_contract, wpf_contract
from dotenv import load_dotenv
from io import StringIO
from web3 import Web3, exceptions
from web3.logs import DISCARD
from web3.middleware import geth_poa_middleware

# Post a data request to the post_dr method of the WRB contract
def handle_requestUpdate(
    w3,
    csv_filename,
    router,
    contract,
    isRouted,
    latestRequestId,
    web3_symbol,    
    web3_from,
    web3_gas,
    web3_gas_price,
    web3_provider_waiting_secs,
    web3_provider_polling_secs
  ):

    try:
      print(f" - Price feed    : {contract.address}")
      print(f" - Price router  : {router.address}")      
      if isRouted == False:
        print(f" - Witnet address: {contract.functions.witnet().call()}")
        print(f" - Request hash  : {contract.functions.hash().call().hex()}")
      else:
        print(f" - Routed pairs  : ({contract.functions.getPairsCount().call()})")

      # Check that the account has enough balance
      balance = w3.eth.getBalance(web3_from)
      if balance == 0:
          raise Exception("Master account run out of funds")

      print(f" - Account       : {web3_from}")        
      print(f" - Balance       : {round(balance / 10 ** 18, 5)} {web3_symbol}")

      # Apply gas price strategy, if any
      if web3_gas_price is None:
        web3_gas_price = w3.eth.generateGasPrice()
      print( " - Tx. gas price :", "{:,}".format(web3_gas_price))     
      
      if web3_gas is not None:
        print( " - Tx. gas limit :", "{:,}".format(web3_gas))

      # Estimate evm+witnet fee
      fee = contract.functions.estimateUpdateFee(web3_gas_price).call()
      print(f" - Tx. value     : {round(fee / 10 ** 18, 5)} {web3_symbol}")

      # Send Web3 transaction ..
      if web3_gas is None:
        # .. without a gas limit
        tx = contract.functions.requestUpdate().transact({
          "from": web3_from,
          "gasPrice": web3_gas_price,
          "value": fee
        })
      else:
        # .. with the gas limit specified in config file        
        tx = contract.functions.requestUpdate().transact({
          "from": web3_from,
          "gas": web3_gas,
          "gasPrice": web3_gas_price,
          "value": fee
        })

      # Log send transaction attempt
      log_master_balance(csv_filename, web3_from, balance, tx.hex())
      print(f" ~ Tx. hash      : {tx.hex()}")      

      # Wait for tx receipt and print relevant tx info upon reception
      receipt = w3.eth.wait_for_transaction_receipt(
        tx,
        web3_provider_waiting_secs,
        web3_provider_polling_secs
      )
      total_fee = balance - w3.eth.getBalance(web3_from)
      print( " > Tx. block num.:", "{:,}".format(receipt.get("blockNumber")))
      print( " > Tx. total gas :", "{:,}".format(receipt.get("gasUsed")))
      print( " > Tx. total fee :", round(total_fee / 10 ** 18, 5), web3_symbol)

    except exceptions.TimeExhausted:
      print(f"   ** Transaction is taking too long !!")
      return [ 0 ]

    except Exception as ex:
      print(f"   xx Transaction rejected: {ex}")
      return [ 0 ]

    # Check if transaction was succesful
    if receipt['status'] == False:
      print(f"   $$ Transaction reverted !!")
      return [ -1, tx.hex() ]
    else:
      requestId = 0
      logs = contract.events.PriceFeeding().processReceipt(receipt, errors=DISCARD)
      if len(logs) > 0:        
        requestId = logs[0].args.queryId
        if requestId > 0:
          print(f" <<<< Request id : {requestId}")
        else:
          print(f" <<<< Synchronous update.")
        return [ requestId, tx.hex(), total_fee ]
      else:
        print(f" ==== Previous request id : {latestRequestId} (nothing to update)")
        return [ latestRequestId, tx.hex(), total_fee ]

def log_master_balance(csv_filename, addr, balance, txhash):
  if csv_filename is not None:
    try:
      with open(csv_filename, "a", encoding="utf-8") as csv_file:
        readable_ts = datetime.datetime.fromtimestamp(int(time.time())).strftime('%Y-%m-%d %H:%M:%S %Z')
        row = f"\"{os.path.splitext(os.path.basename(csv_filename))[0]}\";\"{addr}\";\"{readable_ts}\";\"{balance}\";\"{txhash}\""
        csv_file.write(row + '\n')
    except Exception as ex:
      return

def log_exception_state(addr, reason):
  # log the error and wait 1 second before next iteration
  print(f"Exception while getting state from contract {addr}:\n{reason}")
  time.sleep(1)

@contextlib.contextmanager
def stdoutIO(stdout=None):
  old = sys.stdout
  if stdout is None:
    stdout = StringIO()
  sys.stdout = stdout
  yield stdout
  sys.stdout = old

def dry_run_request(bytecode, timeout_secs):
  cmdline = "npx witnet-toolkit try-data-request --hex "
  cmdline += bytecode.hex()
  cmdline += " | tail -n 2 | head -n 1 | awk -F: '{ print $2 }' | sed 's/ //g' | tr -d \"│\""
  
  # Dry-run result needs to be fetched from temporary file, 
  # because of https://bugs.python.org/issue30154.
  with open("tmp.out", "w+") as output:
    process = subprocess.Popen(
      cmdline,
      stdout = output,
      shell = True,
    )
    process.wait(timeout=timeout_secs)

  with open("tmp.out", "r") as output:
    if os.stat("tmp.out").st_size == 0:
      raise Exception(f"Timeout while trying data request ({timeout_secs} secs)")
    return int(output.read())

def avg_fees(pfs):
  total_fees = 0
  total_records = 0
  for pf in pfs:
    if len(pf["fees"]) > 0:
      total_fees += sum(pf["fees"])
      total_records += len(pf["fees"])
  if total_records > 0:
    return total_fees / total_records
  else:
    return 0

def time_to_die_secs(balance, pfs):
  total_speed = 0
  total_avg_fee = avg_fees(pfs)
  for pf in pfs:
    if len(pf["secs"]) > 0:
      pf_secs = sum(pf["secs"]) / len(pf["secs"])
    else:
      pf_secs = pf["heartbeat"]    
    if pf_secs > 0:
      if len(pf["fees"]) > 0:    
        pf_fee = sum(pf["fees"]) / len(pf["fees"])
      else:
        pf_fee = total_avg_fee
      total_speed += (pf_fee / pf_secs)
  if total_speed > 0:
    return balance / total_speed
  else:
    return 0

def log_loop(
    w3,
    loop_interval_secs,
    csv_filename,
    pfs_config_file_path,
    network_name,
    web3_symbol,
    web3_from,
    web3_gas,
    web3_gas_price,
    web3_finalization_secs,
    web3_max_reverts,
    web3_provider_waiting_secs,
    web3_provider_polling_secs,
    witnet_resolution_secs,
    witnet_toolkit_timeout_secs
  ):
    pfs_config = load_price_feeds_config(pfs_config_file_path, network_name)
    pfs_router = wpr_contract(w3, pfs_config['address'])
    if pfs_router.address is None:
      print("Fatal: no WitnetPriceRouter address")
      exit(1)
    print(f"\nUsing WitnetPriceRouter at {pfs_router.address}:\n")
    
    captionMaxLength = 0
    pfs = []    
    for caption in pfs_config['feeds']:
      erc2362id = pfs_router.functions.currencyPairId(caption).call().hex()
      if pfs_router.functions.supportsCurrencyPair(erc2362id).call():
        print(f"{caption}:")
        for attempt in range(5):
          try:
            addr = pfs_router.functions.getPriceFeed(erc2362id).call()
            if addr == "0x0000000000000000000000000000000000000000":
              print(f"  >< Skipped: not currently supported by this router.")
              break
            contract = wpf_contract(w3, addr)
            cooldown = pfs_config['feeds'][caption].get("minSecsBetweenUpdates", 0)
            deviation = pfs_config['feeds'][caption].get("deviationPercentage", 0.0)
            heartbeat = int(pfs_config['feeds'][caption].get("maxSecsBetweenUpdates", 0))
            routed = pfs_config['feeds'][caption].get("isRouted", False)
            lastPrice = int(contract.functions.lastPrice().call())
            lastTimestamp = contract.functions.lastTimestamp().call()
            latestQueryId = contract.functions.latestQueryId().call()
            if routed == False:
              pendingUpdate = contract.functions.pendingUpdate().call()
              witnet = contract.functions.witnet().call()
            else:
              pendingUpdate = False
              witnet = None
            pfs.append({
              "id": erc2362id,
              "caption": caption,
              "contract": contract,
              "deviation": deviation,
              "heartbeat": heartbeat,
              "isRouted": routed,
              "lastPrice": lastPrice,
              "lastTimestamp": lastTimestamp,
              "latestRequestId": latestQueryId,
              "cooldown": cooldown,
              "pendingUpdate": pendingUpdate,
              "witnet": witnet,
              "reverts": 0,
              "auto_disabled": False,
              "lastRevertedTx": "",
              "fees": [],
              "secs": []
            })
            print(f"  => Witnet address : {witnet}")
            print(f"  => Price feed     : {contract.address}")
            if heartbeat > 0:
              print(f"  => Heartbeat   : {heartbeat} seconds")
            if cooldown > 0:
              print(f"  => Cooldown    : {cooldown} seconds")
            if routed == True:
              print(f"  => Deviation   : (Routed)")
            else:
              print(f"  => Deviation   : {deviation} %")          
            print(f"  => Last price  : {lastPrice / 10 ** int(caption.split('-')[2])} {pfs_config['feeds'][caption]['label']}")
            print(f"  => Last update : {datetime.datetime.fromtimestamp(lastTimestamp).strftime('%Y-%m-%d %H:%M:%S %Z')}")
            print(f"  => Latest id   : {latestQueryId} (pending: {pendingUpdate})\n")
            break
          except Exception as ex:
            if attempt < 4:
              print(f"  >< Attempt #{attempt}: {ex}")
              continue
            else:
              print(f"  >< Skipped: Exception: {ex}")
              break
        
        if len(caption) > captionMaxLength:
          captionMaxLength = len(caption)

      else:
        print(f"{caption} => hashed as {erc2362id}, not found in the registry :/\n")

    if len(pfs) == 0:
      print("Sorry, no price feeds to poll :/")
      return

    print(f"Ok, so let's poll every {loop_interval_secs} seconds...")
    low_balance_ts = int(time.time()) - 900
    total_finalization_secs = web3_finalization_secs + witnet_resolution_secs
    while True:
      print()
      loop_ts = int(time.time())
      
      balance = w3.eth.getBalance(web3_from)
      time_left_secs = time_to_die_secs(balance, pfs)
      if time_left_secs > 0:
        if time_left_secs <= 86400 * 3 and (loop_ts - low_balance_ts) >= 900:
          # start warning every 900 seconds if estimated time before draiing funds is less than 3 days
          low_balance_ts = loop_ts
          print(f"LOW FUNDS !!!: estimated {round(time_left_secs / 3600, 2)} hours before running out of funds")
        else:
          print(f"Time-To-Die: {round(time_left_secs / 3600, 2)} hours")

      for pf in pfs:
        
        contract = pf["contract"]
        caption = pf['caption']
        caption += " " * (captionMaxLength - len(caption))

        # Poll latest update status
        try:
          # Detect eventual pricefeed updates in the router:
          contractAddr = pfs_router.functions.getPriceFeed(pf["id"]).call()
          if contract.address != contractAddr:
            pfs_config = load_price_feeds_config(pfs_config_file_path, network_name)
            print(f"{caption} <> contract route changed from {contract.address} to {contractAddr}")
            contract = wpf_contract(w3, contractAddr)
            pf["contract"] = contract
            if contractAddr != "0x0000000000000000000000000000000000000000":
              for attempt in range(5):
                try:
                  # read from config
                  pf["cooldown"] = int(pfs_config['feeds'][pf['caption']].get("minSecsBetweenUpdates", 0))
                  pf["deviation"] = pfs_config['feeds'][pf['caption']].get("deviationPercentage", 0.0)
                  pf["heartbeat"] = int(pfs_config['feeds'][pf['caption']].get("maxSecsBetweenUpdates", 0))
                  pf["isRouted"] = pfs_config['feeds'][pf['caption']].get("isRouted", False)
                  
                  # read from web3
                  if pf["isRouted"] == False:
                    pf["witnet"] = contract.functions.witnet().call()
                  pf["lastPrice"] = int(contract.functions.lastPrice().call())
                  pf["lastTimestamp"] = contract.functions.lastTimestamp().call()
                  pf["latestQueryId"] = contract.functions.latestQueryId().call()
                  pf["pendingUpdate"] = contract.functions.pendingUpdate().call()

                  # reset flags
                  pf["fees"].clear()
                  pf["secs"].clear()
                  pf["auto_disabled"] = False
                  pf["lastRevertedTx"] = ""
                  pf["reverts"] = 0
                  break
                except Exception as ex:
                  if attempt < 4:
                    print(f"{caption} >< refreshing contract state attempt #{attempt}: {ex}")
                    time.sleep(1)
                  else:
                    raise ex

          if contractAddr == "0x0000000000000000000000000000000000000000":
            # Nothing to do if router stopped supporting this pricefeed
            continue

          if pf["auto_disabled"]:
            # Skip if this pricefeed is disabled
            print(f"{caption} >< too many reverts: see last reverted tx: {pf['lastRevertedTx']}")
            continue

          lastValue = contract.functions.lastValue().call()
          status = lastValue[3]
          current_ts = int(time.time())
          elapsed_secs = current_ts - pf["lastTimestamp"]
        
          # If still waiting for an update...
          if pf["pendingUpdate"] == True:
          
            # A new valid result has just been detected:
            if status == 200 and lastValue[1] > pf["lastTimestamp"]:
              pf["pendingUpdate"] = False
              pf["lastPrice"] = lastValue[0]
              elapsed_secs = lastValue[1] - pf["lastTimestamp"] 
              pf["lastTimestamp"] = lastValue[1]
              print(f"{caption} << drTxHash: {lastValue[2].hex()}, lastPrice updated to {lastValue[0]}, after {elapsed_secs} secs")
              
            # An invalid result has just been detected:
            elif status == 400:
              pf["pendingUpdate"] = False
              latestDrTxHash = contract.functions.latestUpdateDrTxHash().call()
              latestError = contract.functions.latestUpdateErrorMessage().call()
              print(f"{caption} >< drTxHash: {latestDrTxHash.hex()}, latestError: \"{str(latestError)}\", after {elapsed_secs} secs")

            else:
              print(f"{caption} .. contract {contract.address} awaits response from {pf['witnet']}::{pf['latestRequestId']}")
              
          # If no update is pending:
          else :
            
            if elapsed_secs >= pf["cooldown"] - total_finalization_secs:
              last_price = pf["lastPrice"]
              deviation = 0

              if pf["heartbeat"] == 0:
                # No heartbeat, no polling.                
                # But still, watch for external updates on unmanaged routed price feeds could still be traced:                  
                pf["pendingUpdate"] = contract.functions.pendingUpdate().call()
                if pf["pendingUpdate"]:
                  print(f"{caption} <> detected routed update on contract {contract.address}")
                else:
                  print(f"{caption} .. no routed update detected on contract {contract.address}")
                continue

              elif elapsed_secs >= pf["heartbeat"] - (0 if pf["isRouted"] else total_finalization_secs):
                # Otherwise, check heartbeat condition, first:
                reason = f"of heartbeat and Witnet latency"

              elif pf['isRouted'] == False and pf['deviation'] > 0 and last_price > 0:                
                # If heartbeat condition is not met yet, then check for deviation, if required:
                try:
                  next_price = dry_run_request(
                    contract.functions.bytecode().call(),
                    witnet_toolkit_timeout_secs
                  )
                except Exception as ex:
                  # ...if dry run fails, assume 0 deviation as to, at least, guarantee the heartbeat periodicity is met
                  print(f"{caption} >< Dry-run failed:", ex)
                  continue
                deviation = round(100 * ((next_price - last_price) / last_price), 2)
                
                # If deviation is below threshold...
                if abs(deviation) < pf["deviation"]:
                  # ...skip request update until, at least, another `loop_interval_secs` secs
                  print(f"{caption} .. {deviation} % deviation after {elapsed_secs} secs since last update")                  
                  continue
                else:
                  reason = f"deviation is greater than {pf['deviation']} %"

              else:
                external_update = False
                if pf['isRouted'] == True:
                  # Check for update signalling on cached-routed price feeds                
                  external_update = contract.functions.pendingUpdate().call()
                  
                if external_update:
                  reason = f"a routed update was detected"
                else:
                  print(f"{caption} .. awaiting routed update, or heartbeat condition, for another {pf['heartbeat'] - elapsed_secs} secs")
                  continue
                
              print(f"{caption} >> Requesting update after {elapsed_secs} seconds because {reason}:")
              result = handle_requestUpdate(
                w3,
                csv_filename,
                pfs_router,
                contract,
                pf['isRouted'],
                pf['latestRequestId'],
                web3_symbol,
                web3_from,
                web3_gas,
                web3_gas_price,
                web3_provider_waiting_secs,
                web3_provider_polling_secs
              )
              latestRequestId = result[0]
              if latestRequestId > 0:
                pf["latestRequestId"] = latestRequestId
                pf["pendingUpdate"] = True
                pf["reverts"] = 0

              elif latestRequestId < 0:
                pf["lastRevertedTx"] = result[1]
                pf["reverts"] = pf["reverts"] + 1
                if pf["reverts"] >= web3_max_reverts:
                  pf["auto_disabled"] = True

              # on fully successfull update request:
              if len(result) >= 3:                

                # update fees and secs history
                latestFee = result[2]
                if latestFee > 0:
                  pf["fees"].append(latestFee)
                  if len(pf["fees"]) > 16:
                    del pf["fees"][0]
                pf["secs"].append(elapsed_secs)                
                if len(pf["secs"]) > 256:
                  del pf["secs"][0]

                # and in case of routed priced, update lastTimestamp immediately
                if pf["isRouted"]:
                  lastValue = contract.functions.lastValue().call()
                  pf["lastTimestamp"] = lastValue[1]
                  print(f" <<<< lastPrice was {lastValue[0]}, {int(time.time()) - lastValue[1]} secs ago")

            else:
              secs_until_next_check = pf['cooldown'] - elapsed_secs - total_finalization_secs
              if secs_until_next_check > 0:
                print(f"{caption} .. resting for another {secs_until_next_check} secs before next triggering check")
        
        # Capture exceptions while reading state from contract
        except Exception as ex:
          print(f"{caption} .. Exception when getting state from contract {contract.address}: {ex}")
      
      # Sleep just enough between loops
      preemptive_secs = loop_interval_secs - int(time.time()) + loop_ts
      if preemptive_secs > 0:
        time.sleep(preemptive_secs)

def main(args):    
    print("================================================================================")
    print(load_version())
    load_dotenv()

    # Read network parameters from environment:
    network_name = os.getenv('WPFP_NETWORK_NAME')
    network_timeout_secs = int(os.getenv('WPFP_NETWORK_TIMEOUT_SECS') or 60)

    # Read web3 parameters from environment:
    web3_finalization_secs = int(os.getenv('WPFP_WEB3_FINALIZATION_SECS') or 60)
    web3_from = os.getenv('WPFP_WEB3_FROM')
    web3_gas = int(os.getenv('WPFP_WEB3_GAS')) if os.getenv('WPFP_WEB3_GAS') else None
    web3_gas_price = int(os.getenv('WPFP_WEB3_GAS_PRICE')) if os.getenv('WPFP_WEB3_GAS_PRICE') else None
    web3_max_reverts = int(os.getenv('WPFP_WEB3_MAX_REVERTS') or 3)
    web3_provider = args.provider if args.provider else os.getenv('WPFP_WEB3_PROVIDER')
    web3_provider_poa = bool(os.getenv('WPFP_WEB3_PROVIDER_POA'))
    web3_provider_waiting_secs = int(os.getenv('WPFP_WEB3_PROVIDER_WAITING_TIMEOUT_SECS') or 130)
    web3_provider_polling_secs = int(os.getenv('WPFP_WEB3_PROVIDER_POLLING_LATENCY_SECS') or 13)
    web3_symbol = os.getenv('WPFP_WEB3_SYMBOL') or "ETH"

    # Read witnet parameters from environment:
    witnet_resolution_secs = int(os.getenv('WPFP_WITNET_RESOLUTION_SECS') or 300)
    witnet_toolkit_timeout_secs = int(os.getenv('WPFP_WITNET_TOOLKIT_TIMEOUT_SECS') or 15)

    # Echo timers set-up:
    print(f"Loop interval period  : {'{:,}'.format(args.loop_interval_secs)}\"")
    print(f"Web3 finalization time: {'{:,}'.format(web3_finalization_secs)}\"")
    print(f"Witnet resolution time: {'{:,}'.format(witnet_resolution_secs)}\"")
    print(f"Witnet toolkit timeout: {'{:,}'.format(witnet_toolkit_timeout_secs)}\"")

    # Read pricefeeds config path, and config itself:
    config_path = args.json_path if args.json_path else os.getenv('WPFP_CONFIG_PATH')
    if config_path is None:
      print(f"Fatal: no config path was set!")
      exit(1)
    elif load_price_feeds_config(config_path, network_name) is None:
      print(f"Fatal: no configuration available for network '{network_name}'")
      exit(1)
    
    # Create Web3 object
    w3 = Web3(Web3.HTTPProvider(
      web3_provider,
      request_kwargs={'timeout': network_timeout_secs}
    ))

    # Inject POA middleware, if necessary
    if web3_provider_poa:
      w3.middleware_onion.inject(geth_poa_middleware, layer=0)
      print(f"Injected geth_poa_middleware.")

    # Apply appropiate gas price strategy if no integer value is specified in `gas_price`
    if not isinstance(web3_gas_price, int):      
      # If network is Ethereum mainnet, and "estimate_medium" is specied as `gas_price`, try to activate `medium_gas_price_strategy`
      if web3_gas_price == "estimate_medium":        
        if w3.eth.chainId == 1:
          from web3 import middleware
          from web3.gas_strategies.time_based import medium_gas_price_strategy

          # Transaction mined within 5 minutes
          w3.eth.setGasPriceStrategy(medium_gas_price_strategy)

          # Setup cache because get price is slow (it needs 120 blocks)
          w3.middleware_onion.add(middleware.time_based_cache_middleware)
          w3.middleware_onion.add(middleware.latest_block_based_cache_middleware)
          w3.middleware_onion.add(middleware.simple_cache_middleware)

          web3_gas_price = None
          print("Gas price strategy: estimate_medium")
    
        else:          
          # "estimate_medium" strategy not supported in networks other than Ethereum mainnet
          print(f"Invalid gas price: {web3_gas_price}. \"estimate_medium\" can only be used for mainnet (current id: {w3.eth.chainId})")
          exit(1)
      
      # If no `gas_price` value is specified at all, try to activate general RPC gas price strategy:
      elif web3_gas_price is None:
        from web3.gas_strategies.rpc import rpc_gas_price_strategy
        w3.eth.set_gas_price_strategy(rpc_gas_price_strategy)
        print("Gas price strategy: eth_gasPrice")

      # Exit if anything other text is specified in `gas_price`,   
      else:
        print(f"Invalid gas price: {web3_gas_price}.")
        exit(1)

    else:    
      print(f"Gas price strategy: invariant ({'{:,}'.format(web3_gas_price)})")

    # Connect to the Web3 provider
    try:
      current_block = w3.eth.blockNumber
      print(f"Connected to '{network_name}' at block #{current_block} via {web3_provider}")      

    except Exception as ex:
      print(f"Fatal: connection failed to {web3_provider}: {ex}")
      exit(1)

    # Log Web3 client version
    try:
      print(f"Web3 client: {w3.clientVersion}")
    except Exception as ex:
      print(f"RPC provider does not support web3_clientVersion method.")

    # Enter infinite loop
    log_loop(
      w3,
      args.loop_interval_secs,
      args.csv_file,
      config_path,
      network_name,
      web3_symbol,
      web3_from,
      web3_gas,
      web3_gas_price,      
      web3_finalization_secs,
      web3_max_reverts,
      web3_provider_waiting_secs,
      web3_provider_polling_secs,
      witnet_resolution_secs,
      witnet_toolkit_timeout_secs
    )

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Connect to an Ethereum provider.')
    parser.add_argument('--json_path', dest='json_path', action='store', required=False,
                    help='provide path to price feeds configuration file')
    parser.add_argument('--loop_interval_secs', dest='loop_interval_secs', action='store', type=int, required=False, default=30,
                    help='seconds after which the script triggers the state of the smart contract')
    parser.add_argument('--provider', dest='provider', action='store', required=False,
                    help='web3 provider to which the poller should connect. If not provided it reads from config')
    parser.add_argument('--csv_file', dest='csv_file', action='store', required=False, default="",
                    help='provide the CSV file in which master address balance will be logged after sending every new transaction')

    args = parser.parse_args()
    main(args)
