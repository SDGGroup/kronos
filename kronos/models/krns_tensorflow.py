import copy
import datetime
import logging
import os

import mlflow
import numpy as np
import pandas as pd
import tensorflow as tf
from mlflow.tracking import MlflowClient

logger = logging.getLogger(__name__)


class KRNSTensorflow:
    """
    Class to implement Tensorflow in kronos.
    """

    def __init__(
        self,
        modeler,  # TODO: How to explicit its data type without incur in [...] most likely due to a circular import
        model: tf.keras.Sequential = None,
        nn_type: str = "rnn",
        n_units: int = 128,
        activation: str = "relu",
        epochs: int = 25,
        n_inputs: int = 30,
    ) -> None:
        """
        Initialization method.

        :param Modeler modeler: The Modeler instance used to interact with data.
        :param tf.keras.Sequential model: An already fitted Sequential model, to instantiate a kronos Tensorflow model from an already fitted one.
        :param str nn_type: Type of neural network; might be one of the following: 'rnn', 'gru', 'lstm'.
        :param int n_units: Number of units in the main layer of the model.
        :param str activation: Activation function in the main layer of the model.
        :param int epochs: Number of epochs to train the model for.
        :param int n_inputs: Number of lag considered for the training of one step ahead.

        :return: No return.

        **Example**

        .. code-block:: python

            model = KRNSTensorflow(
                    modeler=modeler,
                    nn_type='rnn',
                    n_units=128,
                    activation='relu',
                    epochs=25,
                    n_inputs=30,
                    model=None,
                )

        """
        # Kronos attributes
        self.modeler = copy.deepcopy(modeler)

        # Model attributes
        self.n_inputs = n_inputs
        self.activation = activation
        self.epochs = epochs
        self.n_units = n_units

        # Define nn main layer
        self.nn_type = nn_type
        if self.nn_type == "rnn":
            self.nn_main_layer = tf.keras.layers.SimpleRNN(
                units=self.n_units, activation=self.activation, name="rnn_1"
            )
        elif self.nn_type == "lstm":
            self.nn_main_layer = tf.keras.layers.LSTM(
                units=self.n_units, activation=self.activation, name="lstm_1"
            )
        elif self.nn_type == "gru":
            self.nn_main_layer = tf.keras.layers.GRU(
                units=self.n_units, activation=self.activation, name="gru_1"
            )
        else:
            raise ValueError(f"Neural network type {self.nn_type} not supported.")

        # To load an already configured model
        self.model = model

        self.model_params = {
            "nn_type": self.nn_type,
            "n_inputs": self.n_inputs,
            "activation": self.activation,
            "epochs": self.epochs,
            "n_units": self.n_units,
        }

    def preprocess(self) -> None:
        """
        Get the dataframe into the condition to be processed by the model: transform the data into a numpy array.

        :return: No return.
        """

        try:
            self.modeler.train_data = np.array(
                self.modeler.train_data[self.modeler.metric_col]
            )

        except Exception as e:
            logger.warning(f"### Preprocess train data failed: {e}")

        try:
            self.modeler.test_data = np.array(
                self.modeler.test_data[self.modeler.metric_col]
            )

        except Exception as e:
            logger.warning(f"### Preprocess test data failed: {e}")

    def log_params(self, client: MlflowClient, run_id: str) -> None:
        """
        Log the model params to mlflow.

        :param MlflowClient client: The mlflow client used to log parameters.
        :param str run_id: The run id under which log parameters.

        :return: No return.
        """
        try:
            for key, val in self.model_params.items():
                client.log_param(run_id, key, val)

        except Exception as e:
            logger.error(f"### Log params {self.model_params} failed: {e}")

    def log_model(self, artifact_path: str) -> None:
        """
        Log the model artifact to mlflow.

        :param str artifact_path: Run-relative artifact path.

        :return: No return.
        """
        try:
            # Save the model
            saved_model_path = os.path.join(os.getcwd(), self.modeler.key_code)
            tf.saved_model.save(self.model, saved_model_path)

            # Define graph tags and signature key
            tag = [tf.saved_model.SERVING]
            key = tf.saved_model.DEFAULT_SERVING_SIGNATURE_DEF_KEY

            # TODO: Signature to add before log the model
            mlflow.tensorflow.log_model(
                tf_saved_model_dir=saved_model_path,
                artifact_path=artifact_path,
                tf_meta_graph_tags=tag,
                tf_signature_def_key=key,
            )

            logger.info(f"### Model logged: {self.model}")

        except Exception as e:
            logger.error(f"### Log model {self.model} failed: {e}")

    def fit(self) -> None:
        """
        Instantiate the Sequential model class, compile and fit the model with TimeseriesGenerator.

        :return: No return.
        """
        try:
            # Define the model
            self.model = tf.keras.Sequential(
                [
                    tf.keras.layers.Input(shape=(self.n_inputs, 1), name="input"),
                    self.nn_main_layer,
                    tf.keras.layers.Dense(units=1, name="output"),
                ]
            )

            # Compile the model
            self.model.compile(optimizer="adam", loss="mse")

            # Define generator
            ts_generator = tf.keras.preprocessing.sequence.TimeseriesGenerator(
                data=self.modeler.train_data,
                targets=self.modeler.train_data,
                length=self.n_inputs,
                batch_size=1,
            )

            # Fit the model
            self.model.fit(
                ts_generator, steps_per_epoch=len(ts_generator), epochs=self.epochs
            )

        except Exception as e:
            logger.error(
                f"### Fit with model {self.model} failed: {e} - on data {self.modeler.train_data.head(1)}"
            )

    def predict(
        self,
        n_days: int,
        fcst_first_date: datetime.date = datetime.date.today(),
        future_only: bool = True,
        test: bool = False,
        return_conf_int: bool = False,
    ) -> pd.DataFrame:
        """
        Predict using the fitted model.

        Within the body of the function, the predict method is only called on newly trained models that are still in memory.
        For serialized models from the mlflow model register another prediction method is used.

        Four situations can occur:
            1. fcst_first_date <= last_training_day and difference < n_days (still something to forecast) - Note: all forecasts are predicted by the model.
            2. fcst_first_date << last training day and difference >= n_days (nothing to forecast) - Note: all forecasts are predicted by the model.
            3. fcst_first_date > last training day and some available intermediate data - Note: no update strategy, intermediate data is used to feed the model.
            4. fcst_first_date > last training day and no intermediate data available.

        Since the first step is to keep only historical data (data prior to fcst_first_date, i.e. difference must be at least 1), in each scenario all forecasts are predicted by the model in an autoregressive way by giving the last *n_inputs* of the historical data to the fitted model.

        Finally, depending on the parameter *Modeler.future_only*, it is decided whether to keep only the observations from fcst_first_date onwards or also those in between.

        :param int n_days: Number of data points to predict.
        :param datetime.date fcst_first_date: First date of forecast.
        :param bool future_only: Whether to return predicted missing values between the last observed date and the forecast first date (*False*) or only future values (*True*), i.e. those from the forecast first date onwards.
        :param bool test: Wheter to collect x-reg from test data, or from pred_data


        :return: *(pd.DataFrame)* Pandas DataFrame containing the predictions.
        """

        try:

            # Keep only historic data
            historic_data = self.modeler.data[
                self.modeler.data[self.modeler.date_col] < fcst_first_date
            ]

            # Compute last observed historical day
            last_observed_day = historic_data[self.modeler.date_col].max()

            # Compute the difference between last_observed_day and fcst_first_date
            difference = (fcst_first_date - last_observed_day).days

            # Compute actual forecast horizon
            fcst_horizon = difference + n_days - 1

            # Preprocess historic data
            historic_data = np.array(historic_data[self.modeler.metric_col])

            # Autoregressive prediction
            predictions = []
            batch = historic_data.astype("float32")[-self.n_inputs :].reshape(
                (1, self.n_inputs, 1)
            )
            for i in range(fcst_horizon):
                # Get the prediction value for the first batch: we need to differentiate when we directly use the model after training or when we load it from mlflow.
                if type(self.model) == tf.keras.Sequential:
                    # Model directly used after training
                    pred_val = self.model.predict(batch)[0]
                else:
                    # Model loaded from mlflow model registry
                    # Note: 'input' is the name of the first layer of the network, 'output' the name of the last one
                    pred_val = self.model(input=batch)["output"].numpy()[0]

                # Append the prediction into the array
                predictions.append(pred_val[0])

                # Use the prediction to update the batch and remove the first value
                batch = np.append(batch[:, 1:, :], [[pred_val]], axis=1)

            # Make predictions dataframe
            pred = pd.DataFrame(
                data={
                    self.modeler.date_col: [
                        last_observed_day + datetime.timedelta(days=x)
                        for x in range(1, fcst_horizon + 1)
                    ],
                    self.modeler.fcst_col: predictions,
                }
            )

            # Keep only values from forecast first date onwards (if specified)
            if future_only:
                pred = pred[pred[self.modeler.date_col] >= fcst_first_date]

            return pred

        except Exception as e:
            logger.error(f"### Predict with model {self.model} failed: {e}")
