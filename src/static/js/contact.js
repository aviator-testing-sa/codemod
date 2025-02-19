document.addEventListener('DOMContentLoaded', function() {
  const contactForm = document.getElementById('contactform');

  contactForm.addEventListener('submit', function(event) {
    event.preventDefault();

    const action = this.getAttribute('action');
    const messageDiv = document.getElementById('message');
    const submitButton = document.getElementById('submit');

    // Slide up and hide the message
    messageDiv.style.display = 'none'; // Directly hide the element
    //Using display none instead of slideUp, as slideUp requires jQuery

    // Disable the submit button and add a loading indicator
    submitButton.disabled = true;
    const img = document.createElement('img');
    img.src = 'images/loading.gif';
    img.classList.add('loader');
    submitButton.insertAdjacentElement('afterend', img);


    // Prepare the form data
    const formData = new FormData();
    formData.append('name', document.getElementById('name').value);
    formData.append('email', document.getElementById('email').value);
    formData.append('comments', document.getElementById('comments').value);
    formData.append('verify', document.getElementById('verify').value);


    // Send the POST request using fetch
    fetch(action, {
      method: 'POST',
      body: formData,
    })
    .then(response => response.text())
    .then(data => {
      messageDiv.innerHTML = data;
      messageDiv.style.display = 'block'; // Directly show the element
        //Using display block instead of slideDown, as slideDown requires jQuery

      // Remove the loading indicator
      const loader = contactForm.querySelector('img.loader');
      if (loader) {
        loader.remove(); // Removes the loader image
      }

      // Re-enable the submit button
      submitButton.disabled = false;

      // Optionally slide up the form on success
      if (data.includes('success')) {
        contactForm.style.display = 'none';
         //Using display none instead of slideUp, as slideUp requires jQuery
      }
    })
    .catch(error => {
      console.error('Error:', error);
      messageDiv.innerHTML = 'An error occurred. Please try again.';
      messageDiv.style.display = 'block';
      const loader = contactForm.querySelector('img.loader');
      if (loader) {
        loader.remove(); // Removes the loader image
      }
        submitButton.disabled = false;
    });
  });
});
